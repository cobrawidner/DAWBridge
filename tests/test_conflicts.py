"""Two people publishing at once, and what Dropbox does about it.

Dropbox does not merge and does not ask: one write keeps the name
session.json, the other is renamed to "session (X's conflicted copy
DATE).json" and left there. Before this module existed, nothing in
DAWBridge had any idea that file could be there, and the loser's entire
publish was simply gone with nobody told anything - see
test_the_losers_publish_is_invisible_without_detection for the exact
sequence, which is reproduced here against the real SharedStore.
"""
import json

import pytest

from dawbridge import conflicts, syncstate
from dawbridge.model import Session, Track
from dawbridge.store import SharedStore


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(syncstate, "_STATE_PATH", tmp_path / "state.json")


def _conflict_scenario(folder):
    """Both machines start at r1; both publish; Dropbox keeps A's file and
    sets B's aside. Returns (store, conflicted_path).
    """
    store = SharedStore(folder)
    store.ensure_layout()

    shared = store.load()
    shared.tracks = [Track.new(name="Drums")]
    store.save(shared, updated_by="a@reaper")  # r1, what both machines loaded

    theirs = store.load()
    theirs.tracks.append(Track.new(name="B's vocal comp"))
    theirs.bump("b@protools")  # B's publish: r2

    mine = store.load()
    mine.tracks.append(Track.new(name="A's guitar"))
    store.save(mine, updated_by="a@reaper")  # A's publish also lands on r2

    conflicted = folder / "session (b's conflicted copy 2026-08-09).json"
    conflicted.write_text(theirs.to_json(), encoding="utf-8")
    return store, conflicted


def test_the_losers_publish_is_invisible_without_detection(tmp_path):
    """The failure this module exists for, stated as a fact about the store.

    B's work is not in session.json and not in archive/ either - archive/
    only ever holds versions SharedStore.save() itself replaced, and B's
    was never on disk under the real name. So `dawbridge history` and
    `dawbridge restore` cannot bring it back; the conflicted copy is the
    only copy in existence.
    """
    folder = tmp_path / "shared"
    store, conflicted = _conflict_scenario(folder)

    assert [t.name for t in store.load().tracks] == ["Drums", "A's guitar"]
    archived_names = [
        t.name for _rev, path in store.list_archive() for t in Session.load(path).tracks
    ]
    assert "B's vocal comp" not in archived_names, "the archive cannot save this one"
    assert "B's vocal comp" in conflicted.read_text(encoding="utf-8")


def test_revision_arithmetic_cannot_see_the_conflict(tmp_path):
    """Why this needed its own detector rather than a better drift check.

    Both machines advanced from r1, so both wrote r2. The subtraction in
    describe_drift yields zero from either side - the publish guard isn't
    just absent here, it's structurally blind.
    """
    folder = tmp_path / "shared"
    store, conflicted = _conflict_scenario(folder)
    conflicted.unlink()  # pretend the file isn't there: only the numbers remain

    syncstate.record_sync(folder, "protools", revision=2, action="pull")  # B thinks it published r2
    assert store.load().revision == 2
    assert syncstate.describe_drift(folder, "protools", 2) is None


def test_conflicted_session_copy_is_reported_with_both_sides(tmp_path):
    folder = tmp_path / "shared"
    _conflict_scenario(folder)

    note = conflicts.describe_session_conflict(folder)

    assert note is not None
    assert "b's conflicted copy" in note
    assert "b@protools" in note and "a@reaper" in note, "name both versions' authors"
    assert "history" in note and "restore" in note, "say the archive cannot recover it"


def test_drift_reports_the_conflict_even_when_revisions_match(tmp_path):
    # The whole point: every command already asks describe_drift, so the
    # detection reaches the user with no new command to run.
    folder = tmp_path / "shared"
    _conflict_scenario(folder)
    syncstate.record_sync(folder, "protools", revision=2, action="pull")

    drift = syncstate.describe_drift(folder, "protools", current_revision=2)

    assert drift is not None
    assert "unmerged publish" in drift


def test_drift_reports_conflict_and_ordinary_drift_together(tmp_path):
    folder = tmp_path / "shared"
    _conflict_scenario(folder)
    syncstate.record_sync(folder, "reaper", revision=0, action="pull")

    drift = syncstate.describe_drift(folder, "reaper", current_revision=2)

    assert "unmerged publish" in drift
    assert "2 revisions ahead" in drift, "the normal drift line must not be swallowed"


def test_identical_conflicted_copy_is_not_worth_mentioning(tmp_path):
    # Dropbox does sometimes conflict two byte-identical writes. Nothing
    # was lost, and a warning nobody can act on trains people to skim.
    folder = tmp_path / "shared"
    store = SharedStore(folder)
    store.ensure_layout()
    store.save(Session(name="Song"), updated_by="a@reaper")

    (folder / "session (a's conflicted copy 2026-08-09).json").write_bytes(
        (folder / "session.json").read_bytes()
    )

    assert conflicts.describe_session_conflict(folder) is None


def test_unreadable_conflicted_copy_is_still_reported(tmp_path):
    folder = tmp_path / "shared"
    store = SharedStore(folder)
    store.ensure_layout()
    store.save(Session(name="Song"), updated_by="a@reaper")
    (folder / "session (conflicted copy).json").write_text("{half-writ", encoding="utf-8")

    note = conflicts.describe_session_conflict(folder)

    assert note is not None and "not valid session JSON" in note


def test_recognises_the_wordings_dropbox_actually_uses():
    for name in (
        "session (conflicted copy).json",
        "session (Conner's conflicted copy 2026-08-09).json",
        "session (DESKTOP-A1B2's conflicted copy 2026-08-09).json",
        "session (Case Conflict).json",
        "Shady Grove_stems-001 (conflicted copy 2026-08-09).wav",
    ):
        assert conflicts.is_conflicted_name(name), name

    for name in ("session.json", "session.r0007.json", "My Song (live).wav"):
        assert not conflicts.is_conflicted_name(name), name


def test_original_name_recovers_what_was_conflicted():
    assert conflicts.original_name("session (Conner's conflicted copy 2026-08-09).json") == "session.json"
    assert conflicts.original_name("stem (conflicted copy).wav") == "stem.wav"


def test_audio_conflicts_are_listed_but_do_not_block_publishing(tmp_path):
    # A duplicated audio file wastes space; it isn't lost work, and the
    # content-hash naming means nothing resolves to the wrong file. It
    # belongs in a report, not in the way of a publish.
    folder = tmp_path / "shared"
    store = SharedStore(folder)
    store.ensure_layout()
    store.save(Session(), updated_by="a@reaper")
    (folder / "audio" / "abc123_stem (conflicted copy).wav").write_bytes(b"RIFF....")

    assert conflicts.describe_session_conflict(folder) is None
    assert any("stem.wav" in line for line in conflicts.describe_conflicts(folder))


def test_scan_never_walks_into_the_audio_tree(tmp_path, monkeypatch):
    # Audio lives in Dropbox and can be cloud-only; recursing into it (or
    # reading any of it) would start hydrating gigabytes. Listing names is
    # the only thing allowed.
    folder = tmp_path / "shared"
    store = SharedStore(folder)
    store.ensure_layout()
    deep = folder / "audio" / "peaks" / "nested"
    deep.mkdir(parents=True)
    (deep / "x (conflicted copy).reapeaks").write_bytes(b"x")

    opened = []
    real_open = open

    def watched_open(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", watched_open)
    conflicts.find_conflicts(folder)

    assert opened == [], "detection must be listing-only"


def test_a_conflicted_copy_survives_json_round_trip(tmp_path):
    # Belt and braces: the file we point the user at must really be a
    # loadable session, not something they'll be told to rename over
    # canonical only to find it broken.
    folder = tmp_path / "shared"
    _conflict_scenario(folder)
    path = conflicts.find_session_conflicts(folder)[0]

    recovered = Session.load(path)

    assert recovered.updated_by == "b@protools"
    assert json.loads(path.read_text(encoding="utf-8"))["revision"] == recovered.revision

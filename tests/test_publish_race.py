"""The gap between deciding to publish and publishing.

Every guard runs against the session as it was when it loaded. Reading a
DAW then takes seconds to minutes. A partner publishing anywhere inside
that window passed every check and was overwritten silently - the one
person who needed to know was the one who saw nothing.
"""
import pytest

from dawbridge import syncstate
from dawbridge.cli import main
from dawbridge.model import Session, Track
from dawbridge.store import SharedSessionMoved, SharedStore


def test_save_refuses_when_canonical_moved_under_us(tmp_path):
    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    mine = store.load()
    loaded_at = mine.revision

    partner = store.load()
    partner.tracks = [Track.new(name="Their work")]
    store.save(partner, updated_by="partner@protools")

    with pytest.raises(SharedSessionMoved) as caught:
        store.save(mine, updated_by="me@reaper", expected_revision=loaded_at)

    assert caught.value.found > caught.value.expected
    assert [t.name for t in store.load().tracks] == ["Their work"], "must not overwrite"


def test_save_without_expected_revision_is_unchecked(tmp_path):
    # restore_archived and other deliberate overwrites pass nothing.
    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    mine = store.load()
    store.save(Session(name="Theirs"), updated_by="partner")

    store.save(mine, updated_by="me")  # no expected_revision - allowed

    assert store.load().updated_by == "me"


class _SlowBackend:
    """A DAW read that a partner publishes during."""

    def __init__(self, store):
        self.store = store

    def is_available(self):
        return True

    def project_identity(self):
        return r"C:\p\Song.rpp"

    def read_live_state(self):
        return []

    def pull(self, session, store, warnings=None):
        landed = self.store.load()
        landed.tracks = [Track.new(name="Their late arrival")]
        self.store.save(landed, updated_by="partner@protools")
        session.tracks = [Track.new(name="Mine")]
        return session

    def push(self, session, store):
        return []


def _setup(tmp_path, monkeypatch):
    folder = tmp_path / "shared"
    store = SharedStore(folder)
    store.ensure_layout()
    seed = store.load()
    seed.tracks = [Track.new(name="Shared")]
    store.save(seed, updated_by="partner@protools")
    monkeypatch.setattr(syncstate, "_STATE_PATH", tmp_path / "state.json")
    syncstate.record_sync(folder, "reaper", store.load().revision, "publish", r"C:\p\Song.rpp")
    monkeypatch.setattr("dawbridge.cli._get_backend", lambda daw: _SlowBackend(store))
    return folder, store


def test_cli_publish_refuses_a_publish_that_raced(tmp_path, monkeypatch, capsys):
    folder, store = _setup(tmp_path, monkeypatch)

    code = main(["push", "--daw", "reaper", "--folder", str(folder)])

    assert code == 1
    err = capsys.readouterr().err
    assert "refusing to publish" in err and "while your DAW was being read" in err
    assert [t.name for t in store.load().tracks] == ["Their late arrival"]


def test_force_publishes_over_a_race_on_purpose(tmp_path, monkeypatch):
    folder, store = _setup(tmp_path, monkeypatch)

    assert main(["push", "--daw", "reaper", "--folder", str(folder), "--force"]) == 0
    assert [t.name for t in store.load().tracks] == ["Mine"]

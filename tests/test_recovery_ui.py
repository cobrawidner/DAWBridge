"""Recovery has to be reachable from the app.

The archive keeps 20 revisions, but reaching them meant a terminal and a
Python install - which the person most likely to need them, the
collaborator running the .exe, doesn't have. A safety net nobody can
reach isn't one.

These test the logic behind the buttons rather than the widgets: Tk needs
a display, and what matters is that the GUI and `dawbridge doctor` can't
disagree, and that a restore can't be aimed at something harmful.
"""
from pathlib import Path

from dawbridge import checks
from dawbridge.gui import _worth_restoring
from dawbridge.model import Clip, Session, Track
from dawbridge.store import SharedStore


def _published(tmp_path, tracks=2, audio=True):
    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    if audio:
        (store.audio_dir / "take.wav").write_bytes(b"RIFF....WAVEfmt ")
    session = store.load()
    session.tracks = [Track.new(name=f"Track {i}") for i in range(tracks)]
    for track in session.tracks:
        track.clips = [Clip.new(name="take", start_seconds=0.0, length_seconds=4.0,
                                audio_file="take.wav" if audio else "")]
    store.save(session, updated_by="partner@reaper")
    return store


def test_the_gui_and_doctor_see_the_same_folder(tmp_path):
    # One implementation behind both, so they can't drift into
    # disagreeing about whether a folder is healthy.
    store = _published(tmp_path, audio=False)
    problems, _orphans = checks.check_folder(store)
    assert any("no audio at all" in p for p in problems)


def test_a_healthy_folder_reports_nothing(tmp_path):
    store = _published(tmp_path)
    problems, orphans = checks.check_folder(store)
    assert problems == [] and orphans == []


def test_audio_kept_only_for_an_archived_revision_is_not_called_junk(tmp_path):
    # Deleting this would hollow out the restore it exists to support.
    store = _published(tmp_path)
    session = store.load()
    session.tracks = []
    store.save(session, updated_by="partner@reaper")

    _problems, orphans = checks.check_folder(store)

    assert "take.wav" not in [name for name, _size in orphans]


def test_genuinely_unreferenced_audio_is_reported_biggest_first(tmp_path):
    store = _published(tmp_path)
    (store.audio_dir / "small.wav").write_bytes(b"x" * 100)
    (store.audio_dir / "large.wav").write_bytes(b"x" * 5000)

    _problems, orphans = checks.check_folder(store)

    assert [name for name, _ in orphans] == ["large.wav", "small.wav"]


def test_the_empty_starting_session_is_not_offered_as_a_restore_target(tmp_path):
    # Two clicks from "something went wrong" to an empty shared session,
    # for no benefit, is not a recovery option.
    store = _published(tmp_path)
    placeholder = next(path for rev, path in store.list_archive() if rev == 0)
    assert not _worth_restoring(placeholder)


def test_a_real_revision_is_offered(tmp_path):
    store = _published(tmp_path)
    store.save(store.load(), updated_by="partner@reaper")
    real = max(rev for rev, _ in store.list_archive())
    path = dict(store.list_archive())[real]
    assert _worth_restoring(path)


def test_an_unreadable_revision_is_still_shown(tmp_path):
    # Better a row saying it can't be read than a version that silently
    # isn't in the history at all.
    store = _published(tmp_path)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert _worth_restoring(broken)


def test_describe_revision_identifies_a_version_by_what_is_in_it(tmp_path):
    store = _published(tmp_path, tracks=6)
    store.save(store.load(), updated_by="conner@protools")
    revision, path = store.list_archive()[0]

    line = checks.describe_revision(revision, path)

    assert "6 track(s)" in line and "partner@reaper" in line


def test_describe_revision_survives_a_corrupt_file(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert "unreadable" in checks.describe_revision(3, broken)

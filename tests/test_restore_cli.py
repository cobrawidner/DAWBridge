"""The archive is the only thing standing behind a destructive publish,
so it has to be reachable without hand-editing JSON in Dropbox.
"""
from dawbridge.cli import main
from dawbridge.model import Session, Track
from dawbridge.store import SharedStore


def _publish(store: SharedStore, name: str, tracks: int = 1) -> None:
    session = store.load()
    session.name = name
    session.tracks = [Track.new(name=f"{name} {i}") for i in range(tracks)]
    store.save(session, updated_by="someone@reaper")


def test_history_lists_archived_revisions(tmp_path, capsys):
    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    _publish(store, "Good", tracks=3)
    _publish(store, "Oops", tracks=1)

    assert main(["history", "--folder", str(tmp_path / "shared")]) == 0

    out = capsys.readouterr().out
    assert "archived revisions" in out
    assert "3 track(s)" in out, "the good version should be identifiable by track count"


def test_history_is_calm_about_an_empty_archive(tmp_path, capsys):
    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()

    assert main(["history", "--folder", str(tmp_path / "shared")]) == 0
    assert "nothing archived yet" in capsys.readouterr().out


def test_restore_republishes_an_archived_revision(tmp_path, capsys):
    folder = tmp_path / "shared"
    store = SharedStore(folder)
    store.ensure_layout()
    _publish(store, "Good", tracks=3)
    _publish(store, "Oops", tracks=1)  # partner clobbered it

    good = next(rev for rev, path in store.list_archive()
                if Session.load(path).name == "Good")
    clobbered_at = store.load().revision

    assert main(["restore", "--folder", str(folder), "--revision", str(good)]) == 0

    restored = store.load()
    assert restored.name == "Good"
    assert len(restored.tracks) == 3
    assert restored.revision > clobbered_at, "a restore must move forward, not rewind"


def test_restore_reports_a_revision_that_isnt_there(tmp_path, capsys):
    folder = tmp_path / "shared"
    SharedStore(folder).ensure_layout()

    assert main(["restore", "--folder", str(folder), "--revision", "999"]) == 1
    err = capsys.readouterr().err
    assert "no archived revision r999" in err
    assert "dawbridge history" in err, "tell them how to find a real one"

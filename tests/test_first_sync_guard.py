"""The first five minutes on a new machine.

Handing the .exe to a collaborator creates exactly the state both front
ends used to wave through: a machine with no sync history, pointed at a
folder that already holds someone's real work. One click of Push replaced
all of it. The guard existed and was explicitly suppressed for this case.
"""
from pathlib import Path

from dawbridge import syncstate
from dawbridge.cli import main
from dawbridge.model import Session, Track
from dawbridge.store import SharedStore


class _Backend:
    """Stands in for a DAW holding one nearly-empty project."""

    def __init__(self, project=r"C:\new\Untitled.rpp"):
        self._project = project
        self.pushed = False

    def is_available(self):
        return True

    def project_identity(self):
        return self._project

    def read_live_state(self):
        return []

    def pull(self, session, store, warnings=None):
        session.tracks = [Track.new(name="Audio 1")]
        return session

    def push(self, session, store):
        self.pushed = True
        return []


def _populated(folder: Path) -> SharedStore:
    store = SharedStore(folder)
    store.ensure_layout()
    session = store.load()
    session.name = "Shady Grove"
    session.tracks = [Track.new(name=f"Track {i}") for i in range(6)]
    store.save(session, updated_by="partner@reaper")
    return store


def test_first_ever_publish_onto_real_work_is_refused(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "shared"
    store = _populated(folder)
    monkeypatch.setattr("dawbridge.cli._get_backend", lambda daw: _Backend())
    monkeypatch.setattr(syncstate, "_STATE_PATH", tmp_path / "state.json")

    code = main(["push", "--daw", "reaper", "--folder", str(folder)])

    assert code == 1, "a fresh install must not silently replace existing work"
    assert "refusing to publish" in capsys.readouterr().err
    assert len(store.load().tracks) == 6, "the partner's tracks must still be there"


def test_first_ever_publish_onto_an_empty_folder_is_allowed(tmp_path, monkeypatch):
    # The genuinely new shared folder - nothing to lose, so nothing to ask.
    folder = tmp_path / "shared"
    store = SharedStore(folder)
    store.ensure_layout()
    monkeypatch.setattr("dawbridge.cli._get_backend", lambda daw: _Backend())
    monkeypatch.setattr(syncstate, "_STATE_PATH", tmp_path / "state.json")

    assert main(["push", "--daw", "reaper", "--folder", str(folder)]) == 0
    assert [t.name for t in store.load().tracks] == ["Audio 1"]


def test_force_still_publishes_over_real_work(tmp_path, monkeypatch):
    folder = tmp_path / "shared"
    store = _populated(folder)
    monkeypatch.setattr("dawbridge.cli._get_backend", lambda daw: _Backend())
    monkeypatch.setattr(syncstate, "_STATE_PATH", tmp_path / "state.json")

    assert main(["push", "--daw", "reaper", "--folder", str(folder), "--force"]) == 0
    assert [t.name for t in store.load().tracks] == ["Audio 1"]


def test_publish_records_the_project_so_the_swap_guard_can_work(tmp_path, monkeypatch):
    folder = tmp_path / "shared"
    SharedStore(folder).ensure_layout()
    monkeypatch.setattr("dawbridge.cli._get_backend", lambda daw: _Backend())
    monkeypatch.setattr(syncstate, "_STATE_PATH", tmp_path / "state.json")

    main(["push", "--daw", "reaper", "--folder", str(folder)])

    assert syncstate.last_synced(folder, "reaper")["project"] == r"C:\new\Untitled.rpp"
    assert syncstate.describe_project_change(folder, "reaper", r"C:\other\Song.rpp")

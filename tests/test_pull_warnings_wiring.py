"""A pull's warnings have to reach a human.

The backends now collect what a publish notices but can't act on. That's
only worth anything if the front ends actually render it - a warning
raised into a list nobody reads is the same silence as before.
"""
from dawbridge import syncstate
from dawbridge.cli import main
from dawbridge.model import Session, Track, UnsupportedSchemaVersion
from dawbridge.store import SharedStore


class _NoisyBackend:
    def is_available(self):
        return True

    def project_identity(self):
        return r"C:\p\Song.rpp"

    def read_live_state(self):
        return []

    def pull(self, session, store, warnings=None):
        if warnings is not None:
            warnings.append("publishing removes 'Harmony Vox' (2 clips) from the shared session")
            warnings.append("'take_04.wav' is offline in this project")
        session.tracks = [Track.new(name="Kept")]
        return session

    def push(self, session, store):
        return []


def test_cli_pull_prints_what_the_backend_noticed(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "shared"
    SharedStore(folder).ensure_layout()
    monkeypatch.setattr(syncstate, "_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr("dawbridge.cli._get_backend", lambda daw: _NoisyBackend())

    assert main(["pull", "--daw", "reaper", "--folder", str(folder)]) == 0

    out = capsys.readouterr().out
    assert "publishing removes 'Harmony Vox'" in out
    assert "take_04.wav" in out
    assert out.count("[dawbridge][warning]") == 2


def test_a_backend_that_ignores_the_list_still_works(tmp_path, monkeypatch, capsys):
    class _Old(_NoisyBackend):
        def pull(self, session, store, warnings=None):
            session.tracks = [Track.new(name="Kept")]
            return session

    folder = tmp_path / "shared"
    SharedStore(folder).ensure_layout()
    monkeypatch.setattr(syncstate, "_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr("dawbridge.cli._get_backend", lambda daw: _Old())

    assert main(["pull", "--daw", "reaper", "--folder", str(folder)]) == 0
    assert "[dawbridge][warning]" not in capsys.readouterr().out


def test_a_too_new_session_gets_a_sentence_not_a_traceback(tmp_path, capsys):
    # The two collaborators will end up on different versions of the .exe.
    # Whichever command the out-of-date one runs, they get told to update.
    folder = tmp_path / "shared"
    store = SharedStore(folder)
    store.ensure_layout()
    raw = store.session_path.read_text(encoding="utf-8")
    store.session_path.write_text(raw.replace('"schema_version": 1', '"schema_version": 99'),
                                  encoding="utf-8")

    for command in (["status"], ["history"], ["doctor"]):
        code = main(command + ["--folder", str(folder)])
        err = capsys.readouterr().err
        assert code == 1, f"{command[0]} should fail cleanly"
        assert "newer version of DAWBridge" in err, f"{command[0]} should explain itself"
        assert "Traceback" not in err

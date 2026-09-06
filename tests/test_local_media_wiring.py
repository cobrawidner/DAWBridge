"""The local-copy machinery is actually reached, and never blocks a pull.

`localmedia` is tested on its own. What's tested here is the wiring, for
the usual reason: a helper nobody calls passes all its own tests, and a
save step that raises instead of warning turns a working pull into a
failed one.
"""
from pathlib import Path

from dawbridge import localmedia
from dawbridge.cli import _save_project_if_asked
from dawbridge.model import Clip
from dawbridge.reaper_backend import ReaperBackend
from dawbridge.store import SharedStore


def _store_with_audio(tmp_path: Path):
    store = SharedStore(tmp_path / "Shady Grove.dawbridge")
    store.ensure_layout()
    source = tmp_path / "kick.wav"
    source.write_bytes(b"RIFFkick")
    return store, store.import_audio_file(source)


def _clip(audio_file: str) -> Clip:
    return Clip(id="c1", name="Kick", audio_file=audio_file,
                start_seconds=0.0, length_seconds=1.0)


# ---- the backend resolves audio to the local copy ----------------------

def test_a_saved_project_plays_its_own_copy(tmp_path):
    store, audio_file = _store_with_audio(tmp_path)
    project = tmp_path / "local" / "Song.rpp"
    warnings: list[str] = []

    path = ReaperBackend()._local_audio_path(str(project), store, _clip(audio_file),
                                             "Drums", warnings)

    assert path == project.parent / "Audio Files" / audio_file
    assert path.exists()
    assert warnings == []


def test_an_unsaved_project_falls_back_to_the_shared_file(tmp_path):
    """No project file means no folder to copy into. The arrangement
    still has to play - `apply()` is what says why it isn't ideal."""
    store, audio_file = _store_with_audio(tmp_path)
    warnings: list[str] = []

    path = ReaperBackend()._local_audio_path("", store, _clip(audio_file), "Drums", warnings)

    assert path == store.resolve_audio_path(audio_file)
    assert warnings == []


def test_a_failed_copy_warns_and_still_yields_playable_audio(tmp_path):
    """A clip missing from the timeline is a hole the user has to find
    and repair by hand; playing from the shared folder is merely the old
    behaviour. The warning is what keeps the fallback honest."""
    store, _ = _store_with_audio(tmp_path)
    warnings: list[str] = []

    path = ReaperBackend()._local_audio_path(
        str(tmp_path / "local" / "Song.rpp"), store, _clip("not-there.wav"), "Drums", warnings)

    assert path == store.resolve_audio_path("not-there.wav")
    assert len(warnings) == 1
    assert "Drums" in warnings[0] and "shared folder" in warnings[0]


# ---- the CLI save step -------------------------------------------------

class _Reaper:
    """Stands in for a Reaper with nothing saved yet.

    Note there is no path parameter on the prompt. Reaper's Save dialog
    ignores a supplied filename - confirmed live, it opens empty - so
    DAWBridge no longer pretends to choose one.
    """

    project_extension = ".rpp"

    def __init__(self, saved: str = "", fail: bool = False, lands: str = r"C:\chosen\Song.rpp"):
        self._saved, self._fail, self._lands = saved, fail, lands
        self.was_prompted = False

    def project_file(self):
        return self._saved

    def prompt_save_project(self):
        self.was_prompted = True
        if self._fail:
            raise RuntimeError("the project still isn't saved - the Save dialog was cancelled")
        self._saved = self._lands
        return self._saved


class _ProTools:
    """No project_file/prompt_save_project: a session always has a path."""


def test_an_unsaved_project_gets_reapers_own_save_prompt(tmp_path, capsys):
    backend = _Reaper()

    _save_project_if_asked(backend, tmp_path / "Shady Grove.dawbridge", True)

    assert backend.was_prompted
    out = capsys.readouterr().out
    assert "asking where to save" in out
    assert "Audio Files" in out          # and says where the audio will go


def test_an_already_saved_project_is_never_prompted(tmp_path):
    backend = _Reaper(saved=r"C:\Songs\Song.rpp")

    _save_project_if_asked(backend, tmp_path / "Shady Grove.dawbridge", True)

    assert not backend.was_prompted


def test_without_the_flag_nothing_is_opened_but_the_cost_is_stated(tmp_path, capsys):
    """A command line can't interrupt a scripted run with a modal dialog
    in someone's DAW, so it must not raise one uninvited."""
    backend = _Reaper()

    _save_project_if_asked(backend, tmp_path / "Shady Grove.dawbridge", False)

    out = capsys.readouterr().out
    assert not backend.was_prompted
    assert "play from the shared folder" in out
    assert "--save-project" in out


def test_a_cancelled_save_warns_instead_of_stopping_the_pull(tmp_path, capsys):
    backend = _Reaper(fail=True)

    _save_project_if_asked(backend, tmp_path / "Shady Grove.dawbridge", True)

    assert "[dawbridge][warning]" in capsys.readouterr().out


def test_pro_tools_is_never_prompted(tmp_path, capsys):
    """A Pro Tools session cannot exist unsaved, so the probe is for the
    capability rather than for which DAW is selected."""
    _save_project_if_asked(_ProTools(), tmp_path / "Shady Grove.dawbridge", True)

    assert capsys.readouterr().out == ""


def test_the_media_home_reported_follows_where_reaper_actually_put_it(tmp_path, capsys):
    """The whole reason the old flow was wrong: DAWBridge asked for a
    path, Reaper ignored it, and the audio went somewhere else. What is
    reported must come from where the project LANDED."""
    backend = _Reaper(lands=r"D:\Elsewhere\Different Name.rpp")

    _save_project_if_asked(backend, tmp_path / "Shady Grove.dawbridge", True)

    assert r"D:\Elsewhere\Audio Files" in capsys.readouterr().out

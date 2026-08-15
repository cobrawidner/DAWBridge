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
    """Stands in for a Reaper with nothing saved yet."""

    project_extension = ".rpp"

    def __init__(self, saved: str = "", fail: bool = False):
        self._saved, self._fail = saved, fail
        self.asked_to_save = None

    def project_file(self):
        return self._saved

    def save_project_as(self, path):
        self.asked_to_save = path
        if self._fail:
            raise RuntimeError("Reaper would not save the project")
        self._saved = str(path)
        return self._saved


class _ProTools:
    """No project_file/save_project_as: a session always has a path."""


def test_an_unsaved_project_is_saved_where_asked(tmp_path, capsys):
    backend = _Reaper()
    target = tmp_path / "Song.rpp"

    _save_project_if_asked(backend, tmp_path / "Shady Grove.dawbridge", str(target))

    assert backend.asked_to_save == str(target)
    assert "Audio Files" in capsys.readouterr().out


def test_an_already_saved_project_is_left_alone(tmp_path):
    backend = _Reaper(saved=r"C:\Songs\Song.rpp")

    _save_project_if_asked(backend, tmp_path / "Shady Grove.dawbridge", str(tmp_path / "x.rpp"))

    assert backend.asked_to_save is None


def test_without_the_flag_it_suggests_a_path_rather_than_choosing_one(tmp_path, capsys):
    """A command line can't interrupt a scripted run to ask, so it must
    not decide where someone's project lives."""
    backend = _Reaper()

    _save_project_if_asked(backend, tmp_path / "Shady Grove.dawbridge", None)

    out = capsys.readouterr().out
    assert backend.asked_to_save is None
    assert "--save-project-as" in out
    assert "Shady Grove" in out  # the suggestion is named after the project


def test_a_refused_save_warns_instead_of_stopping_the_pull(tmp_path, capsys):
    backend = _Reaper(fail=True)

    _save_project_if_asked(backend, tmp_path / "Shady Grove.dawbridge", str(tmp_path / "Song.rpp"))

    assert "[dawbridge][warning]" in capsys.readouterr().out


def test_pro_tools_is_never_asked_to_save(tmp_path, capsys):
    """A Pro Tools session cannot exist unsaved, so the probe is for the
    capability rather than for which DAW is selected."""
    _save_project_if_asked(_ProTools(), tmp_path / "Shady Grove.dawbridge", str(tmp_path / "x"))

    assert capsys.readouterr().out == ""


def test_the_suggested_name_comes_from_the_shared_folder(tmp_path):
    """Same source the Discord notifications use, so one project reads
    with one name everywhere."""
    suggested = localmedia.default_project_file("Shady Grove", projects_dir=tmp_path)
    assert suggested.name == "Shady Grove.rpp"

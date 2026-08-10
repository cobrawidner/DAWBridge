"""`dawbridge doctor` - "is this folder OK?" without touching a DAW."""
from dawbridge.cli import main
from dawbridge.model import Clip, Session, Track
from dawbridge.store import SharedStore


def _session_with_audio(tmp_path, audio_name="take.wav", write_audio=True):
    folder = tmp_path / "shared"
    store = SharedStore(folder)
    store.ensure_layout()
    if write_audio:
        (store.audio_dir / audio_name).write_bytes(b"RIFF....WAVEfmt ")
    session = store.load()
    track = Track.new(name="Lead Vocal")
    track.clips = [Clip.new(name="take_01", start_seconds=0.0, length_seconds=4.0,
                            audio_file=audio_name)]
    session.tracks = [track]
    store.save(session, updated_by="a@reaper")
    return folder, store


def test_doctor_is_quiet_on_a_healthy_folder(tmp_path, capsys):
    folder, _ = _session_with_audio(tmp_path)

    assert main(["doctor", "--folder", str(folder)]) == 0
    assert "no problems found" in capsys.readouterr().out


def test_doctor_reports_missing_audio_and_who_needs_it(tmp_path, capsys):
    folder, _ = _session_with_audio(tmp_path, write_audio=False)

    assert main(["doctor", "--folder", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "audio missing" in out
    assert "Lead Vocal/take_01" in out, "say which clip needs it, not just the filename"
    assert "Dropbox hasn't finished syncing" in out, "the likeliest cause, not the scariest"


def test_doctor_finds_a_conflicted_copy(tmp_path, capsys):
    folder, store = _session_with_audio(tmp_path)
    rogue = store.load()
    rogue.tracks = [Track.new(name="Their version")]
    (folder / "session (conflicted copy 2026-08-09).json").write_text(
        rogue.to_json(), encoding="utf-8")

    assert main(["doctor", "--folder", str(folder)]) == 1
    assert "conflicted" in capsys.readouterr().out.lower()


def test_doctor_reports_orphaned_audio_without_calling_it_a_fault(tmp_path, capsys):
    folder, store = _session_with_audio(tmp_path)
    (store.audio_dir / "nobody_wants_this.wav").write_bytes(b"x" * 2048)

    code = main(["doctor", "--folder", str(folder)])

    out = capsys.readouterr().out
    assert "nobody_wants_this.wav" in out
    assert "housekeeping" in out
    assert code == 0, "unreferenced audio is not a problem, just information"


def test_doctor_on_an_untouched_folder_says_so(tmp_path, capsys):
    assert main(["doctor", "--folder", str(tmp_path / "brand_new")]) == 0
    assert "nothing has been published here yet" in capsys.readouterr().out

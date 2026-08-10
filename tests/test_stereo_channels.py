"""Pro Tools splits stereo tracks into separate mono files; the bridge
must put them back together.

Confirmed live: a stereo "Synth" track pulled from Pro Tools landed in the
shared folder as a single mono `.R` file. The track still *said* stereo on
the other side, so it looked fine - it had just quietly lost its stereo
image, which is the kind of loss you only notice by listening.
"""
import struct
from pathlib import Path

from dawbridge import audiofile
from dawbridge.protools_backend import (
    ProToolsBackend,
    _channel_sort_key,
    _split_channel_suffix,
)
from dawbridge.store import SharedStore


def _write_mono(path: Path, fill: bytes, bits: int = 16, rate: int = 44100, frames: int = 64) -> None:
    sample_bytes = bits // 8
    data = fill * frames
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * sample_bytes, sample_bytes, bits)
    path.write_bytes(
        b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data)) + b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", len(data)) + data
    )


def test_split_channel_suffix():
    assert _split_channel_suffix("Synth_01.L") == ("Synth_01", "L")
    assert _split_channel_suffix("Synth_01.R") == ("Synth_01", "R")
    assert _split_channel_suffix("Synth_01") == ("Synth_01", "")


def test_channels_sort_left_before_right():
    # Interleaving R before L would silently swap the stereo image.
    assert sorted(["R", "L"], key=_channel_sort_key) == ["L", "R"]


def test_interleave_produces_stereo_with_correct_channel_order(tmp_path):
    left, right, out = tmp_path / "s.L.wav", tmp_path / "s.R.wav", tmp_path / "s.wav"
    _write_mono(left, b"\x11\x22")
    _write_mono(right, b"\x33\x44")

    assert audiofile.write_wav_interleaved([left, right], out)
    assert audiofile.read_wav_fmt(out) == (2, 44100)

    _fmt, chunks = audiofile.wav_chunks(out)
    at, size = next((c[1], c[2]) for c in chunks if c[0] == b"data")
    first_frame = out.read_bytes()[at:at + 4]
    assert first_frame == b"\x11\x22\x33\x44"  # left sample, then right


def test_pull_recombines_split_channels_into_one_stereo_clip(tmp_path):
    left, right = tmp_path / "Synth_01.L.wav", tmp_path / "Synth_01.R.wav"
    _write_mono(left, b"\x11\x22")
    _write_mono(right, b"\x33\x44")

    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()

    stored = ProToolsBackend()._import_clip_audio(
        {"source_files": [str(left), str(right)], "source_file": str(left)}, store
    )

    assert stored, "clip audio should have been imported"
    assert audiofile.read_wav_fmt(store.resolve_audio_path(stored))[0] == 2, (
        "a stereo Pro Tools track must not arrive as mono"
    )


def test_pull_handles_a_normal_single_file_clip(tmp_path):
    src = tmp_path / "mono_take.wav"
    _write_mono(src, b"\x01\x02")

    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()

    stored = ProToolsBackend()._import_clip_audio(
        {"source_files": [str(src)], "source_file": str(src)}, store
    )

    assert audiofile.read_wav_fmt(store.resolve_audio_path(stored)) == (1, 44100)


def test_pull_survives_a_missing_channel_file(tmp_path):
    # Half a stereo pair missing shouldn't blow up the whole pull.
    left = tmp_path / "Synth_01.L.wav"
    _write_mono(left, b"\x11\x22")

    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()

    stored = ProToolsBackend()._import_clip_audio(
        {"source_files": [str(left), str(tmp_path / "gone.R.wav")], "source_file": str(left)}, store
    )

    assert audiofile.read_wav_fmt(store.resolve_audio_path(stored)) == (1, 44100)


def test_text_export_failure_degrades_instead_of_crashing(monkeypatch):
    # Pro Tools raises an internal assertion when exporting session text
    # for a session with no tracks - which is exactly the state of a brand
    # new session, i.e. the moment before someone's very first push. That
    # must not surface as a traceback.
    class _Builder:
        def include_clip_list(self): pass
        def include_track_edls(self): pass
        def all_tracks(self): pass
        def time_type(self, _v): pass
        def export_string(self):
            raise RuntimeError("Assertion in UExportSessionTextCommand.cpp")

    class _Engine:
        def session_sample_rate(self): return 48000
        def session_path(self): return r"C:\sessions\New\New.ptx"
        def export_session_as_text(self): return _Builder()

    clips, muted = ProToolsBackend()._pull_clips_via_text_export(_Engine())

    # None rather than {}: a failure has to stay distinguishable from an
    # empty session, or a pull publishes every track with its clips
    # stripped - see test_text_export_parse.
    assert clips is None and muted == {}

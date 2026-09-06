"""The Pro Tools pull, end to end, against a fake engine.

`pull()` now opens the engine and hands straight to `_pull(engine, ...)`,
mirroring the `_read_live_state` split that already existed - which makes
the whole path testable for the first time. Everything below would
otherwise need a running Pro Tools, and these are exactly the failures
you cannot see by watching one: they all look like a successful pull.
"""
import pytest

from dawbridge import protools_backend
from dawbridge.model import Clip, Session, Track
from dawbridge.protools_backend import ProToolsBackend
from dawbridge.store import SharedStore


class _NativeTrack:
    def __init__(self, name, stereo=True):
        self.name = name
        self.format = "stereo" if stereo else "mono"


class _Builder:
    def __init__(self, text, fail=False):
        self._text, self._fail = text, fail

    def include_clip_list(self): pass
    def include_track_edls(self): pass
    def all_tracks(self): pass
    def time_type(self, _v): pass

    def export_string(self):
        if self._fail:
            raise RuntimeError("Assertion in UExportSessionTextCommand.cpp")
        return self._text


class _Engine:
    """Only the calls _pull actually makes."""

    def __init__(self, tracks, export_text="", export_fails=False, rate=44100):
        self._tracks, self._text, self._fails, self._rate = tracks, export_text, export_fails, rate
        self.renamed_tracks, self.renamed_clips, self.saves = [], [], 0

    def track_list(self): return self._tracks
    def session_sample_rate(self): return self._rate
    def session_path(self): return r"C:\sessions\Shady Grove\Shady Grove.ptx"
    def export_session_as_text(self): return _Builder(self._text, self._fails)
    def save_session(self): self.saves += 1

    def rename_target_track(self, old_name, new_name):
        self.renamed_tracks.append((old_name, new_name))

    def rename_target_clip(self, clip_name, new_name):
        self.renamed_clips.append((clip_name, new_name))


def _export(*tracks_and_clips) -> str:
    """Build a session text export. Each argument is (track name, [(clip
    name, start samples, duration samples)]).
    """
    lines = [
        "SESSION NAME:\tShady Grove",
        "",
        "O N L I N E   C L I P S   I N   S E S S I O N",
        "",
        "CLIP NAME\tFILE NAME",
    ]
    for _name, clips in tracks_and_clips:
        for clip_name, _start, _dur in clips:
            lines.append(f"{clip_name}\t{clip_name.split(' #')[0]}.wav")
    lines += ["", "T R A C K   L I S T I N G"]
    for name, clips in tracks_and_clips:
        lines += [
            f"TRACK NAME:\t{name} (Stereo)",
            "STATE:\t",
            "CHANNEL\tEVENT\tCLIP NAME\tSTART TIME\tEND TIME\tDURATION\tSTATE",
        ]
        for clip_name, start, dur in clips:
            lines.append(f"1\t1\t{clip_name}\t{start}\t{start + dur}\t{dur}\tUnmuted")
        lines.append("")
    return "\n".join(lines)


@pytest.fixture(autouse=True)
def stereo_without_ptsl(monkeypatch):
    # _track_channels imports the PTSL enum; there's no Pro Tools here.
    monkeypatch.setattr(protools_backend, "_track_channels", lambda t: 2 if t.format == "stereo" else 1)


@pytest.fixture
def store(tmp_path):
    s = SharedStore(tmp_path / "shared")
    s.ensure_layout()
    return s


def test_two_tracks_with_the_same_visible_name_keep_their_own_clips(store):
    """Finding 6, end to end.

    Both tracks are called "Gtr" by the musician; they differ only by
    bridge tag. Before the fix, the clip lookup merged them and each
    canonical track came back holding all three clips - the partner
    received a doubled arrangement, and nothing said a word.
    """
    session = Session()
    export = _export(
        ("Gtr #aaaaaaaa", [("Verse #11111111", 0, 44100), ("Chorus #22222222", 44100, 44100)]),
        ("Gtr #bbbbbbbb", [("Solo #33333333", 88200, 44100)]),
    )
    engine = _Engine([_NativeTrack("Gtr #aaaaaaaa"), _NativeTrack("Gtr #bbbbbbbb")], export)

    ProToolsBackend()._pull(engine, session, store)

    assert [t.id for t in session.tracks] == ["aaaaaaaa", "bbbbbbbb"]
    assert [c.name for c in session.tracks[0].clips] == ["Verse", "Chorus"]
    assert [c.name for c in session.tracks[1].clips] == ["Solo"]


def test_a_failed_text_export_does_not_publish_an_empty_arrangement(store):
    """The worst thing this backend could do quietly.

    Every clip it can see comes from the text export. When Pro Tools
    refuses to produce one, treating that as "no clips" published every
    track with its clips stripped - a total wipe of the shared
    arrangement, from a session that still had all of it on screen.
    """
    session = Session()
    track = Track(id="aaaaaaaa", name="Gtr")
    track.clips = [Clip(id="11111111", name="Verse", audio_file="a.wav",
                        start_seconds=0.0, length_seconds=1.0)]
    session.tracks = [track]

    engine = _Engine([_NativeTrack("Gtr #aaaaaaaa")], export_fails=True)
    warnings: list[str] = []

    ProToolsBackend()._pull(engine, session, store, warnings)

    assert [c.name for c in session.tracks[0].clips] == ["Verse"], "clips must survive"
    assert any("left exactly as they were" in w for w in warnings)


def test_a_pull_reports_the_tracks_it_removes_from_the_shared_session(store):
    session = Session()
    session.tracks = [Track(id="aaaaaaaa", name="Gtr"), Track(id="dddddddd", name="Partner's Vox")]

    engine = _Engine([_NativeTrack("Gtr #aaaaaaaa")], _export(("Gtr #aaaaaaaa", [])))
    warnings: list[str] = []

    ProToolsBackend()._pull(engine, session, store, warnings)

    assert [t.id for t in session.tracks] == ["aaaaaaaa"]
    assert any("Partner's Vox" in w for w in warnings), "say whose work just left the session"


def test_a_pull_captures_the_sample_rate_and_flags_the_change(store):
    session = Session(sample_rate=48000)
    engine = _Engine([], _export(), rate=44100)
    warnings: list[str] = []

    ProToolsBackend()._pull(engine, session, store, warnings)

    assert session.sample_rate == 44100
    assert any("48000" in w and "44100" in w for w in warnings)


def test_an_untagged_track_is_adopted_and_stamped(store):
    session = Session()
    engine = _Engine([_NativeTrack("Fiddle")], _export(("Fiddle", [])))

    ProToolsBackend()._pull(engine, session, store)

    assert len(session.tracks) == 1
    old, new = engine.renamed_tracks[0]
    assert old == "Fiddle" and new.startswith("Fiddle #")
    assert engine.saves == 1, "tags are lost unless the session is saved"


def test_track_order_follows_pro_tools(store):
    session = Session()
    tracks = [_NativeTrack("Vox #aaaaaaaa"), _NativeTrack("Kick #bbbbbbbb"), _NativeTrack("Bass #cccccccc")]
    engine = _Engine(tracks, _export(("Vox #aaaaaaaa", []), ("Kick #bbbbbbbb", []), ("Bass #cccccccc", [])))

    ProToolsBackend()._pull(engine, session, store)

    assert [(t.name, t.order) for t in session.tracks] == [("Vox", 0), ("Kick", 1), ("Bass", 2)]


def test_adopting_a_clip_never_renames_the_file_on_disk(store):
    """Confirmed live against Pro Tools 2025, and it was really happening.

    `rename_target_clip` defaults to `rename_file=True`, which renames AND
    REWRITES the underlying audio file on disk. The push path was fixed
    for this once - the module docstring records it silently renaming and
    rewriting a real shared audio file - but the pull path was never
    given the same treatment and had been carrying it ever since.

    Measured in a live scratch session: after one pull, Pro Tools' Audio
    Files folder held "stereo_probe #fd13bc6f.wav" instead of
    "stereo_probe.wav", and the file had grown from 864044 to 870160
    bytes. It wasn't just renamed, it was rewritten.

    This matters well beyond a tidy filename: Pro Tools' AddAudio imports
    BY REFERENCE, so a session can legitimately reference audio that
    lives anywhere - a sample library, or the shared Google Drive folder. A
    pull would rename and rewrite whatever it found there.
    """
    calls = []

    class _RecordingEngine(_Engine):
        def rename_target_clip(self, clip_name, new_name, rename_file=True):
            calls.append({"clip_name": clip_name, "new_name": new_name, "rename_file": rename_file})

    engine = _RecordingEngine(
        [_NativeTrack("Gtr #aaaaaaaa")],
        _export(("Gtr #aaaaaaaa", [("stereo_probe", 0, 48000)])),
    )

    ProToolsBackend()._pull(engine, Session(), store)

    assert calls, "the clip should have been adopted"
    assert calls[0]["rename_file"] is False, (
        "adopting a clip must never touch the audio file on disk"
    )


def test_one_clip_that_cannot_be_tagged_does_not_abort_the_publish(store):
    """Found by live test against Pro Tools 2025.

    Adopting an untagged clip renames it to carry the bridge id, and that
    rename was unguarded. A real session threw
    `PT_InvalidParameter (Can't found clip: stereo_probe)` - Pro Tools
    would not rename a clip whose name was ambiguous on the timeline -
    and the exception came straight out of pull(), so the ENTIRE publish
    died and nothing was written. push() had this exact failure fixed
    once ("one bad clip aborting an entire push"); the pull side never
    got the same treatment.
    """
    class _RefusingEngine(_Engine):
        def rename_target_clip(self, clip_name, new_name):
            raise RuntimeError("PT_InvalidParameter (Can't found clip: stereo_probe)")

    export = _export(
        ("Gtr #aaaaaaaa", [("stereo_probe", 0, 48000), ("keeper", 96000, 48000)]),
    )
    engine = _RefusingEngine([_NativeTrack("Gtr #aaaaaaaa")], export)
    session = Session()
    warnings: list[str] = []

    ProToolsBackend()._pull(engine, session, store, warnings)

    assert [c.name for c in session.tracks[0].clips] == ["stereo_probe", "keeper"], (
        "both clips must still be published"
    )
    assert any("could not be tagged" in w for w in warnings)
    assert any("stereo_probe" in w for w in warnings)


def test_a_track_that_cannot_be_renamed_does_not_abort_the_publish(store):
    class _RefusingEngine(_Engine):
        def rename_target_track(self, old_name, new_name):
            raise RuntimeError("PT_InvalidParameter")

    engine = _RefusingEngine([_NativeTrack("Fiddle")], _export(("Fiddle", [])))
    session = Session()
    warnings: list[str] = []

    ProToolsBackend()._pull(engine, session, store, warnings)

    assert len(session.tracks) == 1
    assert any("could not be tagged" in w for w in warnings)


def test_pull_works_without_a_warnings_list(store):
    # The optional out-parameter must stay optional.
    engine = _Engine([_NativeTrack("Gtr #aaaaaaaa")], _export(("Gtr #aaaaaaaa", [])))
    assert ProToolsBackend()._pull(engine, Session(), store) is not None

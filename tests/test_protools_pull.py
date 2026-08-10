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


def test_pull_works_without_a_warnings_list(store):
    # The optional out-parameter must stay optional.
    engine = _Engine([_NativeTrack("Gtr #aaaaaaaa")], _export(("Gtr #aaaaaaaa", [])))
    assert ProToolsBackend()._pull(engine, Session(), store) is not None

"""Fields the schema carries must either survive the round trip or say
they didn't.

Anything captured and then silently dropped is worse than not capturing
it: the model claims a fidelity it doesn't have, and the person on the
other end has no way to know which half of their session arrived.
"""
from pathlib import Path

from dawbridge.model import Clip, Session, Track
from dawbridge.sync import (
    describe_meter_mismatch,
    describe_sample_rate_mismatch,
    renumber_tracks,
    should_reimport_audio,
)
from dawbridge.store import SharedStore


# ---- Track.order ------------------------------------------------------

def test_renumber_tracks_gives_every_track_its_real_position():
    tracks = [Track.new(name=n) for n in ("Kick", "Snare", "Bass", "Vox")]
    renumber_tracks(tracks)
    assert [t.order for t in tracks] == [0, 1, 2, 3]


def test_track_order_was_previously_identical_for_a_whole_pull():
    """The bug renumber_tracks fixes, stated as the thing that used to
    happen: Track.new(order=len(session.tracks)) inside a pull, where
    session.tracks doesn't grow until the pull ends, numbers every track
    the same. The real shared session has all six tracks at "order": 6.
    """
    session = Session()
    session.tracks = [Track.new(name=f"old{i}") for i in range(6)]

    pulled = [Track.new(name=n, order=len(session.tracks)) for n in ("A", "B", "C")]
    assert [t.order for t in pulled] == [6, 6, 6], "this is what it used to produce"

    renumber_tracks(pulled)
    assert [t.order for t in pulled] == [0, 1, 2]


def test_status_ordering_follows_the_daw_after_renumbering():
    # `dawbridge status` and the GUI both sort by .order; with every value
    # equal they were only right by accident (a stable sort).
    tracks = [Track.new(name=n) for n in ("Vox", "Kick", "Bass")]
    renumber_tracks(tracks)
    assert [t.name for t in sorted(tracks, key=lambda t: t.order)] == ["Vox", "Kick", "Bass"]


# ---- Session.sample_rate ---------------------------------------------

def test_sample_rate_mismatch_is_described():
    session = Session(sample_rate=44100)
    note = describe_sample_rate_mismatch(session, 48000, "Pro Tools")
    assert note is not None
    assert "48000" in note and "44100" in note


def test_sample_rate_match_is_silent():
    assert describe_sample_rate_mismatch(Session(sample_rate=48000), 48000, "Reaper") is None


def test_sample_rate_unknown_is_silent():
    # A DAW that won't tell us must not produce a warning about a number
    # we invented.
    assert describe_sample_rate_mismatch(Session(sample_rate=48000), None, "Reaper") is None
    assert describe_sample_rate_mismatch(Session(sample_rate=48000), 0, "Reaper") is None


# ---- time signature ---------------------------------------------------

def test_meter_mismatch_is_described_because_neither_daw_can_be_told():
    # Pull captures the meter; nothing applies it. Reaper's push sets the
    # tempo and not the meter, and PTSL has no meter command at all - so a
    # 6/8 song arrives at the right BPM on a 4/4 grid.
    session = Session(time_signature_numerator=6, time_signature_denominator=8)
    note = describe_meter_mismatch(session, 4, 4)
    assert note is not None
    assert "6/8" in note and "4/4" in note


def test_meter_match_is_silent():
    assert describe_meter_mismatch(Session(), 4, 4) is None


def test_meter_unreadable_is_silent():
    assert describe_meter_mismatch(Session(), 0, 0) is None


# ---- Clip.audio_file --------------------------------------------------

def test_reimport_not_needed_when_the_daw_plays_canonical_audio(tmp_path):
    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    src = tmp_path / "take.wav"
    src.write_bytes(b"audio")
    rel = store.import_audio_file(src)

    clip = Clip.new(name="Take", audio_file=rel, start_seconds=0, length_seconds=1)

    assert not should_reimport_audio(str(store.resolve_audio_path(rel)), clip, store)


def test_reimport_needed_when_the_audio_under_a_tagged_clip_was_replaced(tmp_path):
    """The silent revert.

    Pull refreshed a known clip's name/position/length but never its
    audio, so a re-record or "apply track FX to items" published as no
    change at all - and because push re-points takes to whatever canonical
    names, the next push into that same project put the OLD file back
    under the user's new edit, without a word.
    """
    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    old = tmp_path / "old_take.wav"
    old.write_bytes(b"old audio")
    clip = Clip.new(name="Vox", audio_file=store.import_audio_file(old),
                    start_seconds=0, length_seconds=4)

    replaced = tmp_path / "project" / "vox_retake.wav"
    replaced.parent.mkdir()
    replaced.write_bytes(b"new audio")

    assert should_reimport_audio(str(replaced), clip, store)

    clip.audio_file = store.import_audio_file(Path(str(replaced)))
    assert store.resolve_audio_path(clip.audio_file).read_bytes() == b"new audio"
    assert not should_reimport_audio(str(store.resolve_audio_path(clip.audio_file)), clip, store)


def test_reimport_needed_when_canonical_has_no_audio_recorded(tmp_path):
    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    clip = Clip.new(name="Vox", audio_file="", start_seconds=0, length_seconds=1)
    assert should_reimport_audio(r"D:\project\vox.wav", clip, store)


def test_reimport_stays_quiet_without_a_source_path_or_store(tmp_path):
    # A DAW that doesn't report where its audio comes from must not cause
    # a re-import of something we can't identify.
    store = SharedStore(tmp_path / "shared")
    clip = Clip.new(name="Vox", audio_file="x.wav", start_seconds=0, length_seconds=1)
    assert not should_reimport_audio("", clip, store)
    assert not should_reimport_audio(r"D:\project\vox.wav", clip, None)

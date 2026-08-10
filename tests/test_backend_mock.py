from dawbridge.backend import MockBackend
from dawbridge.model import Clip, Session, Track


def test_push_creates_missing_track_then_matches_on_second_push():
    backend = MockBackend()
    session = Session()
    track = Track.new(name="Lead Vocal")
    session.tracks.append(track)

    warnings = backend.push(session, store=None)
    assert warnings == []
    assert track.id in backend.tracks
    assert any("create track" in log for log in backend.pushed_log)

    # Second push of the same (unchanged) session shouldn't try to
    # recreate the track - it should just be a no-op update.
    backend.pushed_log.clear()
    backend.push(session, store=None)
    assert not any("create track" in log for log in backend.pushed_log)


def test_push_adds_new_clip_and_reports_orphan():
    backend = MockBackend()
    session = Session()
    track = Track.new(name="Kick")
    clip = Clip.new(name="Hit", audio_file="hit.wav", start_seconds=0, length_seconds=1)
    track.clips.append(clip)
    session.tracks.append(track)

    backend.push(session, store=None)
    assert clip.id in backend.tracks[track.id]["clip_ids"]

    # Now the clip is removed from canonical (e.g. deleted on the other
    # side) - pushing again must NOT delete it locally, only warn.
    track.clips.clear()
    warnings = backend.push(session, store=None)
    assert clip.id in backend.tracks[track.id]["clip_ids"]  # still there
    assert any(clip.id in w for w in warnings)


def test_pull_adopts_local_only_track():
    backend = MockBackend()
    backend.tracks["deadbeef"] = {"name": "Local Only Track", "clip_ids": set()}

    session = Session()
    session = backend.pull(session, store=None)

    assert len(session.tracks) == 1
    assert session.tracks[0].name == "Local Only Track"
    assert session.tracks[0].id == "deadbeef"

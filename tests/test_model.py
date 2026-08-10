from dawbridge.model import Clip, Session, Track


def test_round_trip_json(tmp_path):
    session = Session(name="Test Session")
    track = Track.new(name="Lead Vocal", order=0)
    track.clips.append(
        Clip.new(name="Verse 1", audio_file="abc123_vocal.wav", start_seconds=4.0, length_seconds=8.0)
    )
    session.tracks.append(track)
    session.bump(updated_by="travis@reaper")

    path = tmp_path / "session.json"
    session.save(path)

    loaded = Session.load(path)
    assert loaded.name == "Test Session"
    assert loaded.revision == 1
    assert loaded.updated_by == "travis@reaper"
    assert len(loaded.tracks) == 1
    assert loaded.tracks[0].name == "Lead Vocal"
    assert loaded.tracks[0].clips[0].name == "Verse 1"
    assert loaded.tracks[0].clips[0].start_seconds == 4.0


def test_track_and_clip_lookup():
    session = Session()
    track = Track.new(name="Kick")
    clip = Clip.new(name="Hit 1", audio_file="x.wav", start_seconds=0, length_seconds=1)
    track.clips.append(clip)
    session.tracks.append(track)

    assert session.track_by_id(track.id) is track
    assert session.track_by_id("nonexistent") is None
    assert track.clip_by_id(clip.id) is clip
    assert track.clip_by_id("nonexistent") is None


def test_ids_are_short_and_unique():
    ids = {Track.new(name="t").id for _ in range(200)}
    assert len(ids) == 200
    assert all(len(i) == 8 for i in ids)

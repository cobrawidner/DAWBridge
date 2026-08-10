"""A pull had no way to tell anyone anything.

`Backend.pull` returned a Session and nothing else, so everything a pull
noticed - media that had gone offline, a duplicated track being re-adopted,
a tempo map that doesn't fit the schema, tracks about to disappear from
the shared session - was swallowed at the point of discovery. The CLI
printed a track count and the GUI printed a track count.

`pull(session, store, warnings=None)` gives it somewhere to put them:
a list the backend appends plain sentences to, ignored when None so no
existing caller breaks.
"""
from dawbridge.backend import Backend, MockBackend
from dawbridge.model import Clip, Session, Track
from dawbridge.store import SharedStore
from dawbridge.sync import (
    describe_dropped_tracks,
    describe_publish_sample_rate_change,
    describe_tempo_map_flattening,
    duplicate_adoption_warning,
)


# ---- the contract -----------------------------------------------------

def test_pull_accepts_a_warnings_list_and_works_without_one():
    backend = MockBackend()
    backend.tracks["aaaaaaaa"] = {"name": "Kick", "clip_ids": set()}

    assert backend.pull(Session(), None) is not None, "must stay callable with no list"

    warnings: list[str] = []
    backend.pull(Session(), None, warnings)
    assert isinstance(warnings, list)


def test_every_backend_advertises_the_same_pull_signature():
    import inspect

    from dawbridge.protools_backend import ProToolsBackend
    from dawbridge.reaper_backend import ReaperBackend

    for cls in (Backend, MockBackend, ReaperBackend, ProToolsBackend):
        params = list(inspect.signature(cls.pull).parameters)
        assert params == ["self", "session", "store", "warnings"], cls.__name__


# ---- tracks silently disappearing from the shared session -------------

def test_publishing_reports_the_tracks_it_removes_from_the_shared_session():
    """Publishing replaces the track list wholesale - deliberate, and
    completely silent. The CLI said "1 new track adopted, 1 total" while
    two of the partner's tracks stopped existing.
    """
    before = [Track.new(name="Vox"), Track.new(name="Drums"), Track.new(name="Bass")]
    before[0].clips.append(Clip.new(name="v", audio_file="a.wav", start_seconds=0, length_seconds=1))
    after = [before[1]]

    warnings = describe_dropped_tracks(before, after)

    assert len(warnings) == 2
    assert any("'Vox'" in w and "1 clip(s)" in w for w in warnings)
    assert all("history" in w for w in warnings), "point at the way back"


def test_no_warning_when_a_publish_keeps_everything():
    tracks = [Track.new(name="Vox")]
    assert describe_dropped_tracks(tracks, list(tracks)) == []


def test_a_renamed_track_is_not_reported_as_dropped():
    # Identity is the id, not the name - a rename must not read as a loss.
    track = Track.new(name="Vox")
    renamed = Track(id=track.id, name="Lead Vocal")
    assert describe_dropped_tracks([track], [renamed]) == []


# ---- redefining the session's sample rate -----------------------------

def test_publishing_at_a_different_rate_is_called_out():
    note = describe_publish_sample_rate_change(44100, 48000)
    assert note is not None and "44100" in note and "48000" in note


def test_no_rate_warning_when_nothing_changes_or_nothing_is_known():
    assert describe_publish_sample_rate_change(48000, 48000) is None
    assert describe_publish_sample_rate_change(None, 48000) is None
    assert describe_publish_sample_rate_change(48000, None) is None


# ---- adopted duplicates ------------------------------------------------

def test_adopting_a_duplicate_explains_the_extra_object():
    note = duplicate_adoption_warning("track", "Vox", "a1b2c3d4")
    assert "Vox" in note and "a1b2c3d4" in note and "copy" in note


# ---- tempo maps -------------------------------------------------------

def test_a_tempo_map_that_cannot_fit_the_schema_is_reported():
    note = describe_tempo_map_flattening(4, 94.0)
    assert note is not None and "4 tempo" in note and "94 BPM" in note


def test_a_single_tempo_marker_is_the_normal_case_and_stays_quiet():
    assert describe_tempo_map_flattening(1, 120.0) is None
    assert describe_tempo_map_flattening(0, 120.0) is None


# ---- offline media -----------------------------------------------------

def test_offline_media_is_reported_rather_than_swallowed(tmp_path):
    """Reaper keeps items whose media has gone offline. The publish used
    to raise out of the middle of the pull loop; then it published them
    as clips with no audio, which was quieter but no better.
    """
    from dawbridge.reaper_backend import _import_audio

    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    warnings: list[str] = []

    assert _import_audio(store, str(tmp_path / "gone.wav"), warnings, "Verse", "Vox") == ""
    assert len(warnings) == 1
    assert "gone.wav" in warnings[0] and "Vox" in warnings[0]

    present = tmp_path / "here.wav"
    present.write_bytes(b"RIFF real")
    assert _import_audio(store, str(present), warnings, "Verse", "Vox").endswith("here.wav")
    assert len(warnings) == 1, "a file that is present must not add noise"

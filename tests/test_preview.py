from dawbridge.backend import LiveClip, LiveTrack
from dawbridge.model import Clip, Session, Track
from dawbridge.sync import preview_push


def _session_with_track(**track_kwargs) -> tuple[Session, Track]:
    session = Session()
    track = Track.new(name="Kick", **track_kwargs)
    session.tracks.append(track)
    return session, track


def test_preview_reports_no_changes_when_daw_already_matches():
    session, track = _session_with_track()
    clip = Clip.new(name="Hit", audio_file="a.wav", start_seconds=1.0, length_seconds=2.0)
    track.clips.append(clip)

    live = [
        LiveTrack(
            bridge_id=track.id,
            name="Kick",
            clips=[LiveClip(bridge_id=clip.id, name="Hit", start_seconds=1.0, length_seconds=2.0)],
        )
    ]

    preview = preview_push(session, live)

    assert preview.is_empty
    assert preview.untouched_clips == 1
    assert "no changes" in preview.summary_line()


def test_preview_detects_new_track_and_its_clips():
    session, track = _session_with_track()
    track.clips.append(Clip.new(name="Hit", audio_file="a.wav", start_seconds=0.0, length_seconds=1.0))

    preview = preview_push(session, live_tracks=[])  # nothing in the DAW yet

    assert [t.kind for t in preview.track_changes] == ["create"]
    assert [c.kind for c in preview.clip_changes] == ["add"]
    assert not preview.is_empty


def test_preview_detects_rename_mute_and_moved_clip():
    session, track = _session_with_track()
    track.muted = True
    clip = Clip.new(name="Hit", audio_file="a.wav", start_seconds=5.0, length_seconds=2.0)
    track.clips.append(clip)

    live = [
        LiveTrack(
            bridge_id=track.id,
            name="Kick OLD",  # renamed locally
            muted=False,       # unmuted locally
            clips=[LiveClip(bridge_id=clip.id, name="Hit", start_seconds=1.0, length_seconds=2.0)],
        )
    ]

    preview = preview_push(session, live)

    kinds = {t.kind for t in preview.track_changes}
    assert kinds == {"rename", "mute"}
    move = next(c for c in preview.clip_changes if c.kind == "move")
    assert move.from_start == 1.0 and move.to_start == 5.0


def test_preview_ignores_subsample_position_noise():
    # A round trip through another DAW's rounding can shift a position by
    # a hair. Reporting that as "moved" would bury real edits in noise.
    session, track = _session_with_track()
    clip = Clip.new(name="Hit", audio_file="a.wav", start_seconds=10.0, length_seconds=2.0)
    track.clips.append(clip)

    live = [
        LiveTrack(
            bridge_id=track.id,
            name="Kick",
            clips=[LiveClip(bridge_id=clip.id, name="Hit", start_seconds=10.0001, length_seconds=2.0)],
        )
    ]

    assert preview_push(session, live).is_empty


def test_preview_reports_orphans_without_planning_deletion():
    session, track = _session_with_track()

    live = [
        LiveTrack(
            bridge_id=track.id,
            name="Kick",
            clips=[LiveClip(bridge_id="deadbeef", name="Local Only", start_seconds=0.0, length_seconds=1.0)],
        )
    ]

    preview = preview_push(session, live)
    orphans = [c for c in preview.clip_changes if c.kind == "orphan"]
    assert len(orphans) == 1
    assert orphans[0].clip_name == "Local Only"


def test_preview_warnings_are_target_specific():
    # Reaper layers overlapping items and loops natively; warning about
    # either when pushing to Reaper is noise, and a warning people learn
    # to skim is worse than no warning at all.
    session, track = _session_with_track()
    track.clips += [
        Clip.new(name="A", audio_file="a.wav", start_seconds=0.0, length_seconds=10.0),
        Clip.new(name="B", audio_file="b.wav", start_seconds=5.0, length_seconds=10.0),
    ]

    assert preview_push(session, [], target="reaper").warnings == []
    assert any("overlap" in w for w in preview_push(session, [], target="protools").warnings)


def test_preview_does_not_warn_about_loop_flag_without_real_looping():
    # Reaper sets "loop source" on items by default, so the flag alone
    # means nothing - only a clip longer than its source truly loops.
    # Without a store to measure the source, stay quiet rather than guess.
    session, track = _session_with_track()
    track.clips.append(
        Clip.new(name="A", audio_file="a.wav", start_seconds=0.0, length_seconds=1.0, loop_source=True)
    )

    assert preview_push(session, [], target="protools", store=None).warnings == []

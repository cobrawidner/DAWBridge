"""Markers: the format declared them and neither backend implemented them.

Not a silent loss like the rest of this codebase's history - a silent
absence. `Session.markers` has always been in the schema and `status` has
always had a section to print them in, and nothing ever put one there.
Verse and chorus positions are exactly what you need to make sense of
someone else's arrangement.

Identity works the same way as tracks and clips: a bridge id embedded in
the DAW-native name ("Chorus #a1b2c3d4"). The alternative - matching on
name or position - means the second push adds every marker again, which
is the -01/-02/-03 duplication failure in a new place.
"""
from dawbridge.backend import LiveMarker, LiveTrack
from dawbridge.model import Marker, Session, Track
from dawbridge.sync import plan_markers, preview_push
from dawbridge.tagging import parse_tag, tag


def _session_with_markers(*specs) -> Session:
    session = Session()
    session.markers = [Marker(id=i, name=n, time_seconds=t) for i, n, t in specs]
    return session


# ---- the plan ---------------------------------------------------------

def test_plan_markers_adds_updates_and_reports_orphans():
    session = _session_with_markers(
        ("aaaaaaaa", "Verse", 0.0), ("bbbbbbbb", "Chorus", 32.0), ("cccccccc", "Solo", 64.0)
    )

    plan = plan_markers(session.markers, local_marker_ids={"aaaaaaaa", "bbbbbbbb", "dddddddd"})

    assert [m.name for m in plan.to_add] == ["Solo"]
    assert {m.name for m in plan.to_update} == {"Verse", "Chorus"}
    assert plan.orphaned_ids == ["dddddddd"]


def test_a_second_push_adds_nothing():
    """The test the whole identity decision exists for.

    After the first push, every canonical marker is in the DAW carrying
    its bridge id. The second push must recognise all of them.
    """
    session = _session_with_markers(("aaaaaaaa", "Verse", 0.0), ("bbbbbbbb", "Chorus", 32.0))

    first = plan_markers(session.markers, local_marker_ids=set())
    assert len(first.to_add) == 2, "first push places them"

    # What the DAW looks like once that push has run: names carry the tag.
    in_the_daw = {parse_tag(tag(m.name, m.id))[1] for m in first.to_add}
    second = plan_markers(session.markers, local_marker_ids=in_the_daw)

    assert second.to_add == [], "a second push must not duplicate a single marker"
    assert len(second.to_update) == 2


def test_markers_are_never_deleted_by_a_push():
    # Same guarantee as clips: a marker the shared session doesn't know
    # about is the local user's, and push is non-destructive.
    plan = plan_markers(canonical_markers=[], local_marker_ids={"deadbeef"})
    assert plan.orphaned_ids == ["deadbeef"]
    assert plan.to_add == [] and plan.to_update == []


# ---- the preview ------------------------------------------------------

def test_preview_shows_a_marker_that_would_be_added():
    session = _session_with_markers(("aaaaaaaa", "Chorus", 32.0))

    preview = preview_push(session, live_tracks=[], live_markers=[])

    assert [(m.kind, m.name) for m in preview.marker_changes] == [("add", "Chorus")]
    assert not preview.is_empty, "a push that only adds markers is not 'nothing to do'"
    assert "1 marker(s) added" in preview.summary_line()


def test_preview_shows_a_moved_marker_with_both_positions():
    session = _session_with_markers(("aaaaaaaa", "Chorus", 32.0))
    live = [LiveMarker(bridge_id="aaaaaaaa", name="Chorus", time_seconds=28.0)]

    move = preview_push(session, [], live_markers=live).marker_changes[0]

    assert move.kind == "move"
    assert move.from_time == 28.0 and move.to_time == 32.0


def test_preview_shows_a_renamed_marker():
    session = _session_with_markers(("aaaaaaaa", "Chorus 2", 32.0))
    live = [LiveMarker(bridge_id="aaaaaaaa", name="Chorus", time_seconds=32.0)]

    change = preview_push(session, [], live_markers=live).marker_changes[0]

    assert change.kind == "rename" and "Chorus" in change.detail


def test_preview_is_quiet_when_markers_already_match():
    session = _session_with_markers(("aaaaaaaa", "Chorus", 32.0))
    live = [LiveMarker(bridge_id="aaaaaaaa", name="Chorus", time_seconds=32.0)]

    preview = preview_push(session, [], live_markers=live)

    assert preview.marker_changes == []
    assert preview.is_empty


def test_preview_ignores_subsample_marker_drift():
    # Same reasoning as clips: a round trip through another DAW's
    # rounding must not read as "the marker moved".
    session = _session_with_markers(("aaaaaaaa", "Chorus", 32.0))
    live = [LiveMarker(bridge_id="aaaaaaaa", name="Chorus", time_seconds=32.0001)]

    assert preview_push(session, [], live_markers=live).marker_changes == []


def test_preview_reports_local_markers_it_will_leave_alone():
    session = _session_with_markers(("aaaaaaaa", "Chorus", 32.0))
    live = [
        LiveMarker(bridge_id="aaaaaaaa", name="Chorus", time_seconds=32.0),
        LiveMarker(bridge_id="deadbeef", name="my punch-in", time_seconds=5.0),
    ]

    orphans = [m for m in preview_push(session, [], live_markers=live).marker_changes
               if m.kind == "orphan"]

    assert [m.name for m in orphans] == ["my punch-in"]


def test_untagged_local_markers_are_not_touched_or_reported():
    # A marker the bridge has never adopted is local-only content, the
    # same as an untagged track. It isn't an orphan; it's just theirs.
    session = _session_with_markers(("aaaaaaaa", "Chorus", 32.0))
    live = [
        LiveMarker(bridge_id="aaaaaaaa", name="Chorus", time_seconds=32.0),
        LiveMarker(bridge_id=None, name="scratch", time_seconds=5.0),
    ]

    assert preview_push(session, [], live_markers=live).marker_changes == []


def test_unknown_marker_state_produces_no_marker_lines():
    # A backend that can't read markers reports None, not []. Claiming
    # every marker is missing would be a preview the push then contradicts.
    session = _session_with_markers(("aaaaaaaa", "Chorus", 32.0))

    preview = preview_push(session, [], live_markers=None)

    assert preview.marker_changes == []
    assert preview.is_empty


def test_marker_changes_do_not_leak_into_the_track_change_list():
    # cli.py and gui.py index a dict by TrackChange.kind; an unexpected
    # kind there would be a KeyError in the middle of a push preview.
    session = _session_with_markers(("aaaaaaaa", "Chorus", 32.0))
    session.tracks.append(Track.new(name="Vox"))

    preview = preview_push(session, [LiveTrack(bridge_id=None, name="x")], live_markers=[])

    assert {t.kind for t in preview.track_changes} <= {"create", "rename", "mute", "unmute"}

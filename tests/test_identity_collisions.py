"""Two DAW objects claiming one identity.

Identity lives in the name (`Lead Vocal #a1b2c3d4`), and duplicating a
track or an item copies the name - tag included. So the ordinary act of
duplicating something hands two objects the same bridge id, and every
lookup in the codebase is a dict keyed by that id.
"""
from dawbridge.backend import LiveClip, LiveTrack
from dawbridge.model import Clip, Session, Track
from dawbridge.sync import (
    claim_live_id,
    describe_duplicate_live_ids,
    merge_pulled_track,
    preview_push,
)


def test_claim_live_id_gives_the_id_to_the_first_and_re_adopts_the_copy():
    seen: set[str] = set()
    assert claim_live_id("a1b2c3d4", seen) == "a1b2c3d4"
    assert claim_live_id("a1b2c3d4", seen) is None, "the copy must be adopted as a new object"
    assert claim_live_id(None, seen) is None


def test_duplicated_track_used_to_collapse_into_one_canonical_object():
    """What a pull did before claim_live_id, spelled out.

    merge_pulled_track returns the *same* Track object for both, so the
    canonical list holds it twice and whichever track is read last
    overwrites the other's clip layout - the first copy's arrangement is
    gone from the shared session with nothing reported.
    """
    session = Session()
    track = Track.new(name="Vox")
    track.clips = [Clip.new(name="take", audio_file="a.wav", start_seconds=0.0, length_seconds=4)]
    session.tracks = [track]

    first = merge_pulled_track(session, "Vox", track.id)
    second = merge_pulled_track(session, "Vox", track.id)

    assert first is second, "this is the collapse the fix has to prevent"

    # ...and with the fix, the copy is treated as untagged and gets its own.
    seen: set[str] = set()
    a = merge_pulled_track(session, "Vox", claim_live_id(track.id, seen))
    b = merge_pulled_track(session, "Vox", claim_live_id(track.id, seen))
    assert a is not b
    assert a.id != b.id


def test_preview_warns_when_two_live_tracks_share_an_id():
    live = [
        LiveTrack(bridge_id="a1b2c3d4", name="Vox", clips=[]),
        LiveTrack(bridge_id="a1b2c3d4", name="Vox", clips=[]),
    ]
    warnings = describe_duplicate_live_ids(live)
    assert len(warnings) == 1
    assert "a1b2c3d4" in warnings[0]


def test_preview_warns_when_two_live_clips_share_an_id():
    live = [
        LiveTrack(
            bridge_id="t1t1t1t1",
            name="Vox",
            clips=[
                LiveClip(bridge_id="c1c1c1c1", name="take", start_seconds=0.0, length_seconds=4.0),
                LiveClip(bridge_id="c1c1c1c1", name="take", start_seconds=32.0, length_seconds=4.0),
            ],
        )
    ]
    warnings = describe_duplicate_live_ids(live)
    assert len(warnings) == 1
    assert "c1c1c1c1" in warnings[0] and "Vox" in warnings[0]


def test_duplicate_ids_reach_the_user_through_the_normal_preview():
    session = Session()
    track = Track.new(name="Vox")
    session.tracks.append(track)
    live = [
        LiveTrack(bridge_id=track.id, name="Vox", clips=[]),
        LiveTrack(bridge_id=track.id, name="Vox", clips=[]),
    ]

    preview = preview_push(session, live, target="reaper")

    assert any("same DAWBridge id" in w for w in preview.warnings)


def test_unique_ids_produce_no_noise():
    live = [
        LiveTrack(bridge_id="aaaaaaaa", name="Vox", clips=[]),
        LiveTrack(bridge_id="bbbbbbbb", name="Gtr", clips=[]),
        LiveTrack(bridge_id=None, name="Local only", clips=[]),  # untagged, not a collision
    ]
    assert describe_duplicate_live_ids(live) == []


# ---- two tracks a musician gave the same name -------------------------

class _NativeTrack:
    def __init__(self, name):
        self.name = name


def test_two_tracks_sharing_a_visible_name_are_no_longer_confused():
    """This used to be unfixable-looking and isn't.

    The backend's clip lookup keyed on the tag-stripped name, so "Gtr
    #aaaaaaaa" and "Gtr #bbbbbbbb" merged and each was credited with the
    other's clips. The tag was in the name the whole time - keying on the
    full name separates them with no native clip ids involved.
    """
    from dawbridge.protools_backend import _duplicate_native_names

    tracks = [
        _NativeTrack("Gtr #aaaaaaaa (Stereo)"),
        _NativeTrack("Gtr #bbbbbbbb (Stereo)"),
        _NativeTrack("Vox #cccccccc"),
    ]

    assert _duplicate_native_names(tracks) == {}, "distinct ids, distinct tracks, no warning"


def test_genuinely_identical_track_names_are_still_reported():
    # Pro Tools enforces unique track names so this shouldn't happen, but
    # if it ever does the failure is silent, which is worth two lines.
    from dawbridge.protools_backend import _duplicate_native_names

    tracks = [_NativeTrack("Gtr (Stereo)"), _NativeTrack("Gtr (Stereo)")]

    assert _duplicate_native_names(tracks) == {"Gtr": 2}


def test_no_false_alarm_in_the_preview_about_shared_names():
    # The preview used to warn about this for Pro Tools. Now that the
    # lookup handles it, the warning would be noise about a situation
    # that works.
    session = Session()
    live = [
        LiveTrack(bridge_id="aaaaaaaa", name="Gtr", clips=[]),
        LiveTrack(bridge_id="bbbbbbbb", name="Gtr", clips=[]),
    ]

    for target in ("reaper", "protools"):
        assert not any("named" in w for w in preview_push(session, live, target=target).warnings)

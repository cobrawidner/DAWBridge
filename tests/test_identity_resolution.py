"""Where a bridge id is read from, when there are two places to look.

Identity lives in the DAW-native name (`Lead Vocal #a1b2c3d4`). That is the
only thing two DAWs sharing no identifiers can agree on, and Pro Tools has
no alternative, so it stays the mechanism.

But a name is a thing a musician edits. Tidy `Lead Vocal #a1b2c3d4` back to
`Lead Vocal` and the tag is gone: the next publish adopts the track as new
and the partner collects a duplicate on every sync from then on. Nothing can
warn, because a missing tag is indistinguishable from a genuinely new track.

Reaper can hold the same id somewhere a musician never sees - per-object
extended state, which survives renames and serialises into the .rpp
(confirmed: a track named plain `FadeProbe` with `<EXT> dawbridge_id
a1b2c3d4 </EXT>` underneath it). That makes it a second place to LOOK, not a
second source of truth.

The rule these tests pin: **the name wins whenever it carries a tag.**
Extended state is consulted only when the name has none. Anything else and a
duplicated track - which inherits its original's extended state - could steal
the original's identity.
"""
from dawbridge.sync import resolve_identities


def _ids(decisions):
    return [d.bridge_id for d in decisions]


# ---- the cases that must not change ------------------------------------

def test_a_tagged_track_with_no_extended_state_behaves_as_it_always_has():
    """Folders full of `Lead Vocal #a1b2c3d4` predate this feature. They must
    resolve exactly as before - and get the net written underneath them.
    """
    [d] = resolve_identities([("a1b2c3d4", None, "Lead Vocal")])

    assert d.bridge_id == "a1b2c3d4"
    assert d.source == "name"
    assert d.write_extended_state, "backfill the net under an existing track"
    assert not d.write_name_tag, "the name is already correct - don't rewrite it"
    assert d.warning is None, "this is the ordinary case and must be silent"


def test_name_and_extended_state_agreeing_is_silent_and_writes_nothing():
    [d] = resolve_identities([("a1b2c3d4", "a1b2c3d4", "Lead Vocal")])

    assert d.bridge_id == "a1b2c3d4" and d.source == "name"
    assert not d.write_extended_state and not d.write_name_tag
    assert d.warning is None


def test_an_untagged_track_with_nothing_stored_is_adopted_as_new():
    [d] = resolve_identities([(None, None, "Fiddle")])

    assert d.bridge_id is None, "None means: mint a fresh id and adopt"
    assert d.source == "new"
    assert d.write_name_tag and d.write_extended_state


def test_two_tracks_sharing_a_name_tag_still_resolve_first_come_first_served():
    # Unchanged behaviour: duplicating a tagged track means the copy is
    # adopted as a new object rather than both claiming one identity.
    first, second = resolve_identities([
        ("a1b2c3d4", None, "Vox"),
        ("a1b2c3d4", None, "Vox"),
    ])

    assert first.bridge_id == "a1b2c3d4"
    assert second.bridge_id is None and second.source == "new"
    assert "copy" in (second.warning or "")


# ---- the case the feature exists for -----------------------------------

def test_a_tidied_name_recovers_its_id_from_extended_state():
    """The failure this prevents: someone deletes the tag from the name, the
    next publish adopts the track as new, and the partner collects a
    duplicate every sync from then on.
    """
    [d] = resolve_identities([(None, "a1b2c3d4", "Lead Vocal")])

    assert d.bridge_id == "a1b2c3d4"
    assert d.source == "extended state"
    assert d.write_name_tag, "put the tag back so the other DAW can see it too"
    assert d.warning is not None and "Lead Vocal" in d.warning


# ---- the trap ----------------------------------------------------------

def test_a_duplicate_whose_name_was_tidied_cannot_steal_the_originals_id():
    """The trap, and why the name has to win.

    Duplicating a track copies its extended state as well as its name. Strip
    the tag from the copy's name and it looks like "an untagged track holding
    id X" - exactly like a recovery case - while the original still holds X
    in its name. If extended state could claim first, the COPY would inherit
    the original's history and the original would be republished as a brand
    new track. Resolving names first makes that impossible whatever order
    the DAW reports them in.
    """
    original = ("a1b2c3d4", "a1b2c3d4", "Vox")
    tidied_copy = (None, "a1b2c3d4", "Vox")

    for order, label in (([original, tidied_copy], "original first"),
                         ([tidied_copy, original], "copy first")):
        decisions = resolve_identities(order)

        assert "a1b2c3d4" in _ids(decisions), label
        keeper = next(d for d in decisions if d.bridge_id == "a1b2c3d4")
        assert keeper.source == "name", f"{label}: the named track must keep the id"
        other = next(d for d in decisions if d.bridge_id is None)
        assert other.source == "new", f"{label}: the copy is adopted as new"


def test_extended_state_pointing_at_an_id_already_taken_is_ignored():
    decisions = resolve_identities([
        ("a1b2c3d4", "a1b2c3d4", "Vox"),
        (None, "a1b2c3d4", "Vox copy"),
    ])

    assert _ids(decisions) == ["a1b2c3d4", None]
    assert "already" in (decisions[1].warning or "").lower()


# ---- disagreement ------------------------------------------------------

def test_when_the_two_sources_disagree_the_name_wins_and_says_so():
    """Happens when a track is copied in from another project, or when a
    previous re-tag wrote the name and then failed to write the id. Not
    guessing: the name is what the other DAW and the shared session both
    see, so it is the one that has to be authoritative.
    """
    [d] = resolve_identities([("bbbbbbbb", "a1b2c3d4", "Vox")])

    assert d.bridge_id == "bbbbbbbb"
    assert d.source == "name"
    assert d.write_extended_state, "correct the stored id so they converge"
    assert d.warning is not None
    assert "bbbbbbbb" in d.warning and "a1b2c3d4" in d.warning


def test_the_label_kind_appears_in_warnings_so_clips_dont_read_as_tracks():
    [track] = resolve_identities([(None, "a1b2c3d4", "Vox")], kind="track")
    [clip] = resolve_identities([(None, "a1b2c3d4", "take 3")], kind="clip")

    assert "track" in track.warning
    assert "clip" in clip.warning


# ---- reading and writing the stored id ---------------------------------

def test_reading_a_stored_id_uses_the_shape_reaper_actually_returns():
    """Confirmed live: GetSetMediaTrackInfo_String returns
    [retval, native_id, key, VALUE, set_flag] - the value is at index 3.
    An unset key returns "" there rather than echoing the buffer passed in,
    which is what makes this a real store (unlike EnumProjectMarkers2's
    name field, which only ever echoes).
    """
    from dawbridge.reaper_backend import _read_ext_id

    def present(native_id, key, value, is_set):
        return [1, "(MediaTrack*)0x1", key, "a1b2c3d4", False]

    def unset(native_id, key, value, is_set):
        return [1, "(MediaTrack*)0x1", key, "", False]

    assert _read_ext_id(present, "(MediaTrack*)0x1") == "a1b2c3d4"
    assert _read_ext_id(unset, "(MediaTrack*)0x1") is None


def test_reading_a_stored_id_never_raises():
    # The name is still the mechanism; the net must never be able to break
    # a publish that would otherwise have worked.
    from dawbridge.reaper_backend import _read_ext_id

    def boom(*_a):
        raise RuntimeError("reapy said no")

    def malformed(*_a):
        return [1, "x"]

    assert _read_ext_id(boom, "x") is None
    assert _read_ext_id(malformed, "x") is None


def test_writing_a_stored_id_sets_the_documented_key_and_never_raises():
    from dawbridge.reaper_backend import _EXT_ID_KEY, _write_ext_id

    calls = []
    _write_ext_id(lambda *a: calls.append(a), "(MediaTrack*)0x1", "a1b2c3d4")
    assert calls == [("(MediaTrack*)0x1", _EXT_ID_KEY, "a1b2c3d4", True)]
    assert _EXT_ID_KEY == "P_EXT:dawbridge_id", "matches what serialises into the .rpp"

    def boom(*_a):
        raise RuntimeError("reapy said no")

    _write_ext_id(boom, "x", "a1b2c3d4")  # must not raise

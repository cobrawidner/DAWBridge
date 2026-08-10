"""Reaper project markers.

ReaScript is unambiguous about time here - `AddProjectMarker2` and
`SetProjectMarker2` take positions as doubles in SECONDS, so unlike the
Pro Tools side there's no unit to work out. The risk is elsewhere: reapy
returns ReaScript's out-parameters as a tuple, and which slot holds what
depends on whether that version prepends the C return value. Reading the
position out of the region-end slot would put every marker at 0.0, and
this file's own module docstring already warns that reapy's buffer-style
conventions vary by version.

So the decoding is a pure function with a test, rather than a subscript
buried in a loop nobody can run without Reaper open.
"""
from dawbridge.reaper_backend import _decode_marker_row

# EnumProjectMarkers2 out-params, in ReaScript's documented order:
#   (retval, proj, idx, isrgn, pos, rgnend, name, markrgnindexnumber)
_WITH_RETVAL = (2, 0, 0, False, 32.5, 0.0, "Chorus #bbbbbbbb", 3)
_WITHOUT_RETVAL = (0, 0, False, 32.5, 0.0, "Chorus #bbbbbbbb", 3)


def test_decodes_the_layout_that_includes_the_return_value():
    assert _decode_marker_row(_WITH_RETVAL) == (False, 32.5, "Chorus #bbbbbbbb", 3)


def test_decodes_the_layout_without_the_return_value():
    assert _decode_marker_row(_WITHOUT_RETVAL) == (False, 32.5, "Chorus #bbbbbbbb", 3)


def test_a_region_row_is_identified_as_a_region():
    row = (1, 0, 0, True, 8.0, 24.0, "Verse region", 1)
    is_region, position, name, index = _decode_marker_row(row)
    assert is_region is True
    assert position == 8.0 and name == "Verse region" and index == 1


def test_a_marker_at_zero_is_still_a_marker():
    # 0.0 is falsy; a decoder written with `or` instead of an explicit
    # check would drop the marker at the very start of the song, which is
    # exactly where people put "Intro".
    assert _decode_marker_row((1, 0, 0, False, 0.0, 0.0, "Intro", 1)) == (False, 0.0, "Intro", 1)


def test_a_row_that_makes_no_sense_is_refused_rather_than_misread():
    # Better to report a marker as unreadable than to publish it at a
    # position taken from the wrong field.
    assert _decode_marker_row(()) is None
    assert _decode_marker_row((1, 2, 3)) is None
    assert _decode_marker_row(("not", "a", "marker", "row", "at", "all", "x", "y")) is None


def test_a_boolean_is_not_mistaken_for_a_position():
    # bool is a subclass of int in Python; without an explicit guard a
    # True in the position slot would decode as 1.0 seconds.
    assert _decode_marker_row((1, 0, 0, True, True, 0.0, "x", 1)) is None


def test_an_integer_position_is_accepted():
    # ReaScript hands back doubles, but a wrapper that rounds a whole
    # number to int must not make the row unreadable.
    assert _decode_marker_row((1, 0, 0, False, 8, 0.0, "Bridge", 2)) == (False, 8.0, "Bridge", 2)

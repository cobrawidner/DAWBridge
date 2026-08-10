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


class _FakeRPR:
    """Just the calls read_live_markers makes."""

    def __init__(self, rows, n_markers, n_regions=0, project_path="", dirty=0):
        self._rows, self._n = rows, (n_markers, n_regions)
        self._path, self._dirty = project_path, dirty

    def CountProjectMarkers(self, *_a):
        return [0, 0, self._n[0], self._n[1]]

    def EnumProjectMarkers2(self, _proj, idx, *_a):
        return self._rows[idx]

    def EnumProjects(self, *_a):
        return [0, 0, self._path]

    def IsProjectDirty(self, *_a):
        return self._dirty


def _backend_with(rpr, monkeypatch):
    import sys
    import types

    from dawbridge.reaper_backend import ReaperBackend

    fake = types.ModuleType("reapy")
    fake.reascript_api = rpr
    monkeypatch.setitem(sys.modules, "reapy", fake)
    return ReaperBackend()


def _rpp(tmp_path, *lines) -> str:
    path = tmp_path / "scratch.rpp"
    path.write_text(
        chr(10).join(['<REAPER_PROJECT 0.1 "7.69" 1', *lines, ">"]), encoding="utf-8"
    )
    return str(path)


# reapy reports position and index correctly and the name never.
def _row(idx_pos, position, index, is_region=0):
    return [idx_pos + 1, 0, idx_pos, is_region, position, 0.0, "", index]


def test_names_come_from_the_saved_project(tmp_path, monkeypatch):
    """The whole point: reapy gives position and index, the file gives the
    name, and therefore the bridge tag, and therefore identity.
    """
    path = _rpp(tmp_path, '  MARKER 1 12.5 "Verse #22222222" 0 0 1 R {G} 0 2')
    backend = _backend_with(
        _FakeRPR([_row(0, 12.5, 1)], n_markers=1, project_path=path), monkeypatch
    )
    warnings: list[str] = []

    markers = backend.read_live_markers(warnings)

    assert [(m.name, m.bridge_id, m.time_seconds) for m in markers] == [("Verse", "22222222", 12.5)]
    assert warnings == []


def test_an_unsaved_project_refuses_rather_than_using_stale_names(tmp_path, monkeypatch):
    """A stale name is worse than an admitted absence - a stale bridge tag
    duplicates on the partner's side, which is the failure this design
    exists to prevent. A preview must not save, so it can only read a file
    that is already current.
    """
    path = _rpp(tmp_path, '  MARKER 1 12.5 "Verse #22222222" 0 0 1 R {G} 0 2')
    backend = _backend_with(
        _FakeRPR([_row(0, 12.5, 1)], n_markers=1, project_path=path, dirty=1), monkeypatch
    )
    warnings: list[str] = []

    assert backend.read_live_markers(warnings) is None
    assert "unsaved changes" in warnings[0]
    assert "Tracks and clips are unaffected" in warnings[0]


def test_a_position_disagreement_means_the_file_is_out_of_date(tmp_path, monkeypatch):
    # The one field both sources report. If they disagree the file
    # describes a different state, so its names can't be trusted either.
    path = _rpp(tmp_path, '  MARKER 1 99.0 "Verse #22222222" 0 0 1 R {G} 0 2')
    backend = _backend_with(
        _FakeRPR([_row(0, 12.5, 1)], n_markers=1, project_path=path), monkeypatch
    )
    warnings: list[str] = []

    assert backend.read_live_markers(warnings) is None
    assert "out of date" in warnings[0]


def test_a_marker_missing_from_the_file_is_refused(tmp_path, monkeypatch):
    path = _rpp(tmp_path, '  MARKER 1 12.5 "Verse #22222222" 0 0 1 R {G} 0 2')
    backend = _backend_with(
        _FakeRPR([_row(0, 12.5, 1), _row(1, 40.0, 2)], n_markers=2, project_path=path), monkeypatch
    )
    warnings: list[str] = []

    assert backend.read_live_markers(warnings) is None
    assert "not in the saved file" in warnings[0]


def test_a_never_saved_project_is_admitted_not_guessed(monkeypatch):
    backend = _backend_with(
        _FakeRPR([_row(0, 12.5, 1)], n_markers=1, project_path=""), monkeypatch
    )
    warnings: list[str] = []

    assert backend.read_live_markers(warnings) is None
    assert "never been saved" in warnings[0]


def test_regions_are_left_out_and_reported_on_publish(tmp_path, monkeypatch):
    path = _rpp(tmp_path, '  MARKER 1 12.5 "Verse #22222222" 0 0 1 R {G} 0 2')
    backend = _backend_with(
        _FakeRPR([_row(0, 12.5, 1), _row(1, 40.0, 2, is_region=1)],
                 n_markers=1, n_regions=1, project_path=path),
        monkeypatch,
    )
    warnings: list[str] = []

    markers = backend.read_live_markers(warnings, for_publish=True)

    assert [m.name for m in markers] == ["Verse"], "the region has nowhere to put its end"
    assert any("region" in w for w in warnings)


def test_a_genuinely_unnamed_marker_is_fine(tmp_path, monkeypatch):
    # Reaper lets you make an unnamed marker; it simply has no bridge tag
    # yet and gets adopted like any untagged object.
    path = _rpp(tmp_path, '  MARKER 1 12.5 "" 0 0 1 R {G} 0 2')
    backend = _backend_with(
        _FakeRPR([_row(0, 12.5, 1)], n_markers=1, project_path=path), monkeypatch
    )

    markers = backend.read_live_markers([])

    assert len(markers) == 1 and markers[0].name == "" and markers[0].bridge_id is None


# ---- names come from the saved .rpp ------------------------------------
#
# reapy cannot return a marker's name (proved above and live), so the name
# - and therefore the bridge tag, and therefore identity - is read from
# the serialised project instead. Same pattern, and the same class of
# reason, as _pull_clips_via_text_export on the Pro Tools side: the live
# API physically cannot answer and the serialised form can.

# Captured verbatim from a real Reaper 7.69 save during the live run.
_REAL_RPP = """<REAPER_PROJECT 0.1 "7.69/win64" 1754800000
  RIPPLE 0
  TEMPO 90 6 8 0
  MARKER 1 16.66666666666667 "Verse #22222222" 0 0 1 R {D36A1D5E-8A8D-412D-A6BF-C6716C7AAF05} 0 2
  MARKER 2 42.66666666666666 "Chorus #33333333" 0 0 1 R {647AB13C-101E-4578-ACCF-A348F5B32B14} 0 2
  MARKER 1 53.33333333333334 "A region" 1 0 1 R {4A6485AC-A85B-4408-9726-508DDEAF0438} 0 1
  MARKER 1 64 "" 1
>
"""


def test_parses_marker_names_and_positions_from_a_real_save():
    from dawbridge.reaper_backend import _parse_rpp_markers

    rows = _parse_rpp_markers(_REAL_RPP)

    assert [(r["index"], r["name"]) for r in rows] == [
        (1, "Verse #22222222"),
        (2, "Chorus #33333333"),
    ], "regions and region-end lines must not appear"
    assert rows[0]["position"] == 16.66666666666667


def test_region_lines_are_excluded_by_their_flag():
    # "A region" and its end line both carry isrgn=1. The schema has
    # nowhere to keep a region's end, so publishing one would flatten it.
    from dawbridge.reaper_backend import _parse_rpp_markers

    assert all("region" not in r["name"] for r in _parse_rpp_markers(_REAL_RPP))


def test_marker_names_with_spaces_and_awkward_characters_survive():
    from dawbridge.reaper_backend import _parse_rpp_markers

    text = "\n".join([
        '<REAPER_PROJECT 0.1 "7.69" 1',
        '  MARKER 1 1.0 "Take 2 - the good one #aabbccdd" 0 0 1 R {G} 0 2',
        "  MARKER 2 2.0 '' 0",
        '  MARKER 3 3.0 "" 0',
        ">",
    ])

    rows = _parse_rpp_markers(text)

    assert rows[0]["name"] == "Take 2 - the good one #aabbccdd"
    assert rows[1]["name"] == "" and rows[2]["name"] == ""


def test_a_file_that_is_not_a_reaper_project_is_refused():
    from dawbridge.reaper_backend import _parse_rpp_markers

    assert _parse_rpp_markers("this is not a project") is None
    assert _parse_rpp_markers("") is None

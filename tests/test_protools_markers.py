"""Pro Tools memory locations.

PTSL exposes these properly - `get_memory_locations()` returns structured
messages, not display text - so reading them needs no parsing of a text
export. What it does NOT expose is the UNIT of a location's position:
`CreateMemoryLocationRequestBody` has no time-type field at all (verified
against the installed py-ptsl protobufs), unlike `SpotClipsByID` and
`set_timeline_selection` which both carry an explicit
`TimelineLocationType`.

That single gap decides the whole design here. A misread unit puts every
marker at a confidently wrong position, and a marker in the wrong place
looks exactly like a marker in the right place - so positions that can't
be converted exactly are refused out loud at both ends rather than
approximated.
"""
import pytest

from dawbridge import protools_backend
from dawbridge.model import Marker, Session
from dawbridge.protools_backend import (
    ProToolsBackend,
    _format_pt_time,
    _parse_pt_time,
    _pt_time_style,
)
from dawbridge.store import SharedStore

_TP_MARKER = 1      # TimeProperties.TP_Marker
_TP_SELECTION = 2   # TimeProperties.TP_Selection


class _Location:
    def __init__(self, number, name, start_time, time_properties=_TP_MARKER):
        self.number, self.name = number, name
        self.start_time, self.time_properties = start_time, time_properties


class _Engine:
    def __init__(self, locations=(), rate=48000):
        self.locations = list(locations)
        self._rate = rate
        self.created, self.edited = [], []

    def session_sample_rate(self): return self._rate
    def get_memory_locations(self): return self.locations

    def create_memory_location(self, start_time=None, name=None, **kw):
        self.created.append((name, start_time))
        self.locations.append(_Location(len(self.locations) + 1, name, start_time))

    def edit_memory_location(self, location_number, name, start_time, **kw):
        self.edited.append((location_number, name, start_time))


@pytest.fixture(autouse=True)
def marker_enums(monkeypatch):
    """TimeProperties/MemoryLocationReference come from PTSL_pb2, which
    imports fine here, but pin TP_Marker so the fakes stay readable.
    """
    import ptsl.PTSL_pb2 as pb
    assert pb.TimeProperties.TP_Marker == _TP_MARKER, "fake would not match the real enum"


# ---- the time formats -------------------------------------------------

def test_recognises_only_the_formats_that_round_trip_exactly():
    assert _pt_time_style("1587600") == "samples"
    assert _pt_time_style("1:04.286") == "minsecs"
    assert _pt_time_style("00:01:04:07") is None, "timecode is quantised to whole frames"
    assert _pt_time_style("33|1|000") is None, "bars|beats needs a tempo map we don't have"
    assert _pt_time_style("12+05") is None
    assert _pt_time_style("") is None


def test_samples_convert_exactly_both_ways():
    seconds = _parse_pt_time("1587600", 44100)
    assert seconds == 36.0
    assert _format_pt_time(seconds, "samples", 44100) == "1587600"


def test_minsecs_convert_both_ways():
    assert _parse_pt_time("1:04.286", 48000) == pytest.approx(64.286)
    assert _format_pt_time(64.286, "minsecs", 48000) == "1:04.286"
    assert _parse_pt_time("2:00:30.500", 48000) == pytest.approx(7230.5)


def test_a_format_we_cannot_convert_returns_nothing_rather_than_a_guess():
    assert _parse_pt_time("00:01:04:07", 48000) is None
    assert _format_pt_time(64.0, "timecode", 48000) is None


# ---- reading ----------------------------------------------------------

def test_reads_markers_and_ignores_selections():
    engine = _Engine([
        _Location(1, "Verse #aaaaaaaa", "0"),
        _Location(2, "Chorus #bbbbbbbb", "1536000"),
        _Location(3, "a range", "96000", time_properties=_TP_SELECTION),
    ])

    markers = ProToolsBackend()._read_live_markers(engine)

    assert [(m.name, m.bridge_id, m.time_seconds) for m in markers] == [
        ("Verse", "aaaaaaaa", 0.0),
        ("Chorus", "bbbbbbbb", 32.0),
    ]


def test_a_position_it_cannot_convert_is_dropped_out_loud():
    engine = _Engine([_Location(1, "Chorus", "00:01:04:07")])
    warnings: list[str] = []

    markers = ProToolsBackend()._read_live_markers(engine, warnings)

    assert markers == []
    assert len(warnings) == 1
    assert "Chorus" in warnings[0] and "Min:Secs or Samples" in warnings[0]


def test_an_unreachable_pro_tools_reports_unknown_not_empty():
    class _Broken:
        def get_memory_locations(self): raise RuntimeError("PTSL said no")
        def session_sample_rate(self): return 48000

    warnings: list[str] = []
    assert ProToolsBackend()._read_live_markers(_Broken(), warnings) is None, (
        "None means 'could not ask'; [] would tell the preview every marker is missing"
    )
    assert warnings


# ---- writing ----------------------------------------------------------

def test_writes_markers_in_the_format_pro_tools_itself_uses():
    engine = _Engine([_Location(1, "Existing #aaaaaaaa", "48000")], rate=48000)
    session = Session()
    session.markers = [
        Marker(id="aaaaaaaa", name="Existing", time_seconds=1.0),
        Marker(id="bbbbbbbb", name="Chorus", time_seconds=32.0),
    ]
    warnings: list[str] = []

    ProToolsBackend()._push_markers(engine, session, warnings)

    assert engine.created == [("Chorus #bbbbbbbb", "1536000")], "learned samples from the example"
    assert warnings == []


def test_refuses_to_write_when_there_is_nothing_to_learn_the_format_from():
    """The honest outcome, and why it isn't a shrug.

    With no existing memory location, the unit of the position string is
    unknowable, and there is no safe way to experiment: a wrongly-placed
    marker can only be removed with clear_all_memory_locations, which
    would take the user's own markers with it.
    """
    engine = _Engine([], rate=48000)
    session = Session()
    session.markers = [Marker(id="bbbbbbbb", name="Chorus", time_seconds=64.286)]
    warnings: list[str] = []

    ProToolsBackend()._push_markers(engine, session, warnings)

    assert engine.created == [], "nothing written"
    assert len(warnings) == 1
    assert "'Chorus' at 1:04.286" in warnings[0], "list the positions so it's a minute of work"
    assert "Add any one marker by hand" in warnings[0], "say how to make it work next time"


def test_a_second_push_moves_markers_instead_of_adding_them():
    """The duplication guard, at the backend level.

    The first push creates "Chorus #bbbbbbbb"; the second must recognise
    that tag and edit it in place. Without the bridge id in the name this
    is where -01/-02/-03 duplicates would appear.
    """
    engine = _Engine([_Location(1, "Existing #aaaaaaaa", "48000")], rate=48000)
    session = Session()
    session.markers = [Marker(id="bbbbbbbb", name="Chorus", time_seconds=32.0)]

    backend = ProToolsBackend()
    backend._push_markers(engine, session, [])
    assert len(engine.created) == 1

    session.markers[0].time_seconds = 40.0
    backend._push_markers(engine, session, [])

    assert len(engine.created) == 1, "the second push must not create a second Chorus"
    assert engine.edited and engine.edited[-1][2] == "1920000", "it moved the existing one"


def test_markers_in_pro_tools_but_not_in_canonical_are_left_alone():
    engine = _Engine([_Location(1, "Their note #cccccccc", "48000")], rate=48000)
    warnings: list[str] = []

    ProToolsBackend()._push_markers(engine, Session(), warnings)

    assert engine.created == [] and engine.edited == []
    assert any("left in place" in w for w in warnings)


def test_untagged_local_markers_are_not_claimed_or_reported():
    # Somebody's own punch-in point. Not an orphan, just theirs.
    engine = _Engine([_Location(1, "punch in", "48000")], rate=48000)
    warnings: list[str] = []

    ProToolsBackend()._push_markers(engine, Session(), warnings)

    assert warnings == []


# ---- pulling ----------------------------------------------------------

def test_pulling_adopts_an_untagged_marker_by_stamping_its_name(tmp_path):
    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    engine = _Engine([_Location(1, "Chorus", "1536000")], rate=48000)
    session = Session()

    ProToolsBackend()._pull_markers(engine, session, [])

    assert len(session.markers) == 1
    marker = session.markers[0]
    assert marker.name == "Chorus" and marker.time_seconds == 32.0
    assert engine.edited[0][1] == f"Chorus #{marker.id}", "stamped so the next pull knows it"


def test_pulling_twice_does_not_mint_a_second_marker(tmp_path):
    engine = _Engine([_Location(1, "Chorus #bbbbbbbb", "1536000")], rate=48000)
    session = Session()
    session.markers = [Marker(id="bbbbbbbb", name="Chorus", time_seconds=32.0)]

    ProToolsBackend()._pull_markers(engine, session, [])
    ProToolsBackend()._pull_markers(engine, session, [])

    assert len(session.markers) == 1
    assert session.markers[0].id == "bbbbbbbb"


def test_a_marker_removed_in_pro_tools_is_reported_when_the_publish_drops_it():
    engine = _Engine([], rate=48000)
    session = Session()
    session.markers = [Marker(id="bbbbbbbb", name="Chorus", time_seconds=32.0)]
    warnings: list[str] = []

    ProToolsBackend()._pull_markers(engine, session, warnings)

    assert session.markers == []
    assert any("'Chorus'" in w and "removes them" in w for w in warnings)

"""Marker changes have to show up in the preview.

The rule this codebase holds itself to is that the preview and the push
never disagree. Markers apply on push whether or not the front ends pass
`live_markers`, so failing to wire it would mean a push silently doing
something the preview never mentioned.
"""
from dawbridge import syncstate
from dawbridge.cli import _at, main
from dawbridge.model import Marker, Session, Track
from dawbridge.store import SharedStore
from dawbridge.sync import preview_push


class _Backend:
    """A DAW with nothing in it, so canonical markers all read as 'add'."""

    def __init__(self, markers=None):
        self._markers = markers

    def is_available(self):
        return True

    def project_identity(self):
        return r"C:\p\Song.rpp"

    def read_live_state(self):
        return []

    def read_live_markers(self):
        return self._markers

    def capture(self, session, store, warnings=None):
        return session

    def apply(self, session, store):
        return []


def _seeded(tmp_path):
    folder = tmp_path / "shared"
    store = SharedStore(folder)
    store.ensure_layout()
    session = store.load()
    session.tracks = [Track.new(name="Lead Vocal")]
    session.markers = [Marker.new(name="Chorus", time_seconds=45.5)]
    store.save(session, updated_by="partner@reaper")
    return folder, store


def test_preview_reports_a_marker_the_daw_is_missing(tmp_path, monkeypatch, capsys):
    folder, _ = _seeded(tmp_path)
    monkeypatch.setattr(syncstate, "_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr("dawbridge.cli._get_backend", lambda daw: _Backend(markers=[]))

    assert main(["preview", "--daw", "reaper", "--folder", str(folder)]) == 0

    out = capsys.readouterr().out
    assert "markers:" in out
    assert "Chorus" in out and "45.500s" in out


def test_a_daw_that_cannot_report_markers_says_nothing_about_them(tmp_path, monkeypatch, capsys):
    # None means "couldn't look", which must not be rendered as "all missing".
    folder, _ = _seeded(tmp_path)
    monkeypatch.setattr(syncstate, "_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr("dawbridge.cli._get_backend", lambda daw: _Backend(markers=None))

    assert main(["preview", "--daw", "protools", "--folder", str(folder)]) == 0
    assert "markers:" not in capsys.readouterr().out


def test_a_marker_only_change_is_not_reported_as_nothing_to_do(tmp_path):
    session = Session()
    session.markers = [Marker.new(name="Chorus", time_seconds=45.5)]

    preview = preview_push(session, [], target="reaper", live_markers=[])

    assert not preview.is_empty, "a push that moves only a marker still does something"


def test_a_missing_position_does_not_break_the_listing():
    # Pro Tools can report a marker whose unit couldn't be resolved.
    assert _at(None) == "?"
    assert _at(12.3456) == "12.346s"

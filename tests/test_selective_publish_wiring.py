"""A publish must only touch the tracks you changed.

This is the wiring, not the planning: `sync.plan_publish` is tested on
its own. What's tested here is that the front end takes its snapshots at
the right moment and renders the result honestly, because both are easy
to get subtly wrong and neither shows up as a crash.
"""
from dawbridge import syncstate
from dawbridge.cli import main
from dawbridge.model import Clip, Track
from dawbridge.store import SharedStore


class _Backend:
    """A DAW holding `live`, standing in for whatever the musician has open."""

    def __init__(self, live):
        self._live = live

    def is_available(self):
        return True

    def project_identity(self):
        return r"C:\p\Song.rpp"

    def read_live_state(self):
        return []

    def read_live_markers(self):
        return None

    def capture(self, session, store, warnings=None):
        # Mirrors the real backends: canonical Track objects are reused and
        # mutated in place, which is why a snapshot has to be taken first.
        session.tracks = self._live
        return session

    def apply(self, session, store):
        return []


def _track(name, start=0.0):
    track = Track.new(name=name)
    track.clips = [Clip.new(name="take", start_seconds=start, length_seconds=4.0,
                            audio_file="a.wav")]
    return track


def _shared(tmp_path, tracks):
    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    session = store.load()
    session.tracks = tracks
    store.save(session, updated_by="partner@protools")
    return store


def _sync_state(tmp_path, monkeypatch, store, folder):
    monkeypatch.setattr(syncstate, "_STATE_PATH", tmp_path / "state.json")
    syncstate.record_sync(folder, "reaper", store.load().revision, "load", r"C:\p\Song.rpp")


def test_a_track_you_never_touched_survives_your_publish(tmp_path, monkeypatch, capsys):
    # The headline case. Their track is in the shared session; my DAW has
    # it too, unchanged; I publish. Theirs must not be clobbered - and in
    # the old wholesale world this was the failure that cost real work.
    mine, theirs = _track("Mine"), _track("Theirs")
    store = _shared(tmp_path, [mine, theirs])
    folder = tmp_path / "shared"
    _sync_state(tmp_path, monkeypatch, store, folder)

    edited = Track.new(name="Mine")
    edited.id = mine.id
    edited.clips = [Clip.new(name="take", start_seconds=9.0, length_seconds=4.0,
                             audio_file="a.wav")]
    unchanged = Track.new(name="Theirs")
    unchanged.id = theirs.id
    unchanged.clips = list(theirs.clips)
    monkeypatch.setattr("dawbridge.cli._get_backend",
                        lambda daw: _Backend([edited, unchanged]))

    assert main(["push", "--daw", "reaper", "--folder", str(folder)]) == 0

    names = [t.name for t in store.load().tracks]
    assert "Theirs" in names and "Mine" in names


def test_the_publish_says_what_it_will_do_before_it_does_it(tmp_path, monkeypatch, capsys):
    store = _shared(tmp_path, [_track("Kept")])
    folder = tmp_path / "shared"
    _sync_state(tmp_path, monkeypatch, store, folder)
    monkeypatch.setattr("dawbridge.cli._get_backend",
                        lambda daw: _Backend([_track("Brand New")]))

    main(["push", "--daw", "reaper", "--folder", str(folder)])

    out = capsys.readouterr().out
    assert "publishing will:" in out, "state the plan before acting on it"


def test_a_snapshot_is_taken_before_capture_mutates_canonical(tmp_path, monkeypatch, capsys):
    """The trap that makes selective publishing silently useless.

    `capture()` mutates canonical's Track objects in place. If the
    before-picture is taken afterwards, every track compares equal to
    itself, nothing is ever "changed", and the plan reports no work while
    happily publishing. It fails soft, which is the worst kind.
    """
    original = _track("Vocal", start=0.0)
    store = _shared(tmp_path, [original])
    folder = tmp_path / "shared"
    _sync_state(tmp_path, monkeypatch, store, folder)

    moved = Track.new(name="Vocal")
    moved.id = original.id
    moved.clips = [Clip.new(name="take", start_seconds=30.0, length_seconds=4.0,
                            audio_file="a.wav")]
    monkeypatch.setattr("dawbridge.cli._get_backend", lambda daw: _Backend([moved]))

    main(["push", "--daw", "reaper", "--folder", str(folder)])

    assert store.load().tracks[0].clips[0].start_seconds == 30.0, (
        "a real edit must reach the shared session"
    )
    assert "updated" in capsys.readouterr().out, "and be reported as an update"

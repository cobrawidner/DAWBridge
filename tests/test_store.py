from pathlib import Path

from dawbridge.model import Session, Track
from dawbridge.store import SharedStore


def test_ensure_layout_creates_files(tmp_path):
    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    assert (tmp_path / "shared" / "session.json").exists()
    assert (tmp_path / "shared" / "audio").is_dir()


def test_save_bumps_revision_and_load_round_trips(tmp_path):
    store = SharedStore(tmp_path / "shared")
    session = Session(name="My Session")
    session.tracks.append(Track.new(name="Guitar"))

    store.save(session, updated_by="travis@reaper")
    assert session.revision == 1

    reloaded = store.load()
    assert reloaded.name == "My Session"
    assert reloaded.revision == 1
    assert reloaded.tracks[0].name == "Guitar"


def test_import_audio_file_dedupes_by_hash(tmp_path):
    store = SharedStore(tmp_path / "shared")
    src = tmp_path / "source.wav"
    src.write_bytes(b"fake audio bytes")

    rel1 = store.import_audio_file(src)
    rel2 = store.import_audio_file(src)  # same content again

    assert rel1 == rel2
    assert store.resolve_audio_path(rel1).exists()


def test_import_audio_file_does_not_stack_hash_prefixes(tmp_path):
    # A file that round-trips (shared -> DAW -> pulled back) comes back
    # already named "<hash>_name.wav". Re-importing it must yield the
    # SAME name, not "<hash2>_<hash1>_name.wav" - otherwise every round
    # trip adds a prefix and leaves another full-size copy behind.
    store = SharedStore(tmp_path / "shared")
    src = tmp_path / "source.wav"
    src.write_bytes(b"fake audio bytes")

    rel1 = store.import_audio_file(src)

    # Simulate the DAW handing back the already-imported copy.
    round_tripped = store.resolve_audio_path(rel1)
    rel2 = store.import_audio_file(round_tripped)

    assert rel2 == rel1
    assert len(list((tmp_path / "shared" / "audio").iterdir())) == 1


def test_save_writes_the_immediately_prior_version_as_bak(tmp_path):
    store = SharedStore(tmp_path / "shared")
    store.save(Session(name="First"), updated_by="a")
    store.save(Session(name="Second"), updated_by="b")

    backup = tmp_path / "shared" / "archive" / "session.json.bak"
    assert backup.exists()
    assert Session.from_json(backup.read_text(encoding="utf-8")).name == "First"


def test_save_keeps_a_run_of_past_revisions(tmp_path):
    # One backup deep is shallower than the failure it guards against:
    # publishing over a partner's work takes two saves to become
    # unrecoverable, so by the time anyone notices, a single .bak is
    # already the wrong version.
    store = SharedStore(tmp_path / "shared")
    for name in ("First", "Second", "Third", "Fourth"):
        session = store.load()
        session.name = name
        store.save(session, updated_by="a")

    archived = store.list_archive()
    assert [rev for rev, _ in archived] == [3, 2, 1, 0], "newest first, no gaps"
    assert Session.load(dict(archived)[1]).name == "First"


def test_archive_is_pruned_to_the_keep_limit(tmp_path):
    from dawbridge.store import _ARCHIVE_KEEP

    store = SharedStore(tmp_path / "shared")
    for _ in range(_ARCHIVE_KEEP + 5):
        store.save(store.load(), updated_by="a")

    archived = store.list_archive()
    assert len(archived) == _ARCHIVE_KEEP
    assert archived[0][0] > archived[-1][0], "the oldest are the ones dropped"


def test_restore_archived_republishes_forward(tmp_path):
    # Restoring must move the revision counter forward, not rewind it.
    # Every "did this move since I last looked?" check compares revision
    # numbers, so a rewind would read as "nothing happened" on the other
    # machine - the restore would be invisible to whoever it's for.
    store = SharedStore(tmp_path / "shared")
    for name in ("Good", "Oops"):
        session = store.load()
        session.name = name
        store.save(session, updated_by="a")

    good_revision = next(rev for rev, path in store.list_archive()
                         if Session.load(path).name == "Good")
    before = store.load().revision

    restored = store.restore_archived(good_revision, updated_by="a")

    assert restored.name == "Good"
    assert restored.revision > before
    assert store.load().name == "Good"


def test_restore_archived_is_itself_undoable(tmp_path):
    store = SharedStore(tmp_path / "shared")
    for name in ("Good", "Oops"):
        session = store.load()
        session.name = name
        store.save(session, updated_by="a")

    good_revision = next(rev for rev, path in store.list_archive()
                         if Session.load(path).name == "Good")
    store.restore_archived(good_revision, updated_by="a")

    replaced = tmp_path / "shared" / "archive" / "session.json.bak"
    assert Session.from_json(replaced.read_text(encoding="utf-8")).name == "Oops"


def test_restore_archived_rejects_an_unknown_revision(tmp_path):
    import pytest

    store = SharedStore(tmp_path / "shared")
    store.save(Session(name="Only"), updated_by="a")

    with pytest.raises(FileNotFoundError):
        store.restore_archived(999, updated_by="a")


def test_save_never_writes_a_revision_that_already_existed(tmp_path):
    # The partner published while this machine had the session open -
    # ordinary on a Google Drive folder. Saving a stale in-memory session must
    # not re-issue a revision number canonical has already been past.
    store = SharedStore(tmp_path / "shared")
    store.save(Session(name="Theirs"), updated_by="them")
    store.save(store.load(), updated_by="them")
    ahead = store.load().revision

    stale = Session(name="Mine")  # loaded long ago, still at revision 0
    store.save(stale, updated_by="me")

    assert stale.revision > ahead
    assert store.load().name == "Mine"


def test_write_wav_slice_extracts_exact_region(tmp_path):
    # Pro Tools gets audio pre-sliced to the clip's region so it never
    # needs a post-spot trim (trim_to_selection deletes same-track
    # neighbours). Must work on any WAV format tag, including 32-bit
    # float, which Python's `wave` module can't read at all.
    import struct

    from dawbridge.audiofile import duration_seconds as _audio_duration_seconds
    from dawbridge.audiofile import write_wav_slice

    src = tmp_path / "src.wav"
    channels, rate, bits = 2, 1000, 16
    frames = 4000  # 4 seconds
    frame_bytes = channels * bits // 8
    data = b"\x01\x02" * (frames * channels)
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * frame_bytes, frame_bytes, bits)
    src.write_bytes(
        b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data)) + b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", len(data)) + data
    )
    assert abs(_audio_duration_seconds(src) - 4.0) < 1e-9

    dst = tmp_path / "slice.wav"
    assert write_wav_slice(src, dst, start_seconds=1.0, length_seconds=2.0)
    assert abs(_audio_duration_seconds(dst) - 2.0) < 1e-9

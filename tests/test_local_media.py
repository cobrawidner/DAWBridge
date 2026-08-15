"""The DAW keeps its own copy of the audio, not the shared folder's.

Two things are covered here, and they're one feature: an unsaved project
has no folder, so the save prompt is the prerequisite for the local copy
rather than a separate nicety.

What makes this worth testing rather than eyeballing is that both halves
fail *quietly*. A local copy that silently didn't happen leaves a
project that works perfectly until the day the shared folder moves, and
a path comparison that stopped recognising local copies re-hashes every
stem on every publish while still producing the right answer.
"""
from pathlib import Path

import pytest

from dawbridge import localmedia, sync
from dawbridge.model import Clip
from dawbridge.store import SharedStore


def _store_with_audio(tmp_path: Path, name: str = "kick.wav", data: bytes = b"RIFFkick"):
    store = SharedStore(tmp_path / "shared.dawbridge")
    store.ensure_layout()
    source = tmp_path / name
    source.write_bytes(data)
    return store, store.import_audio_file(source)


# ---- where things go ---------------------------------------------------

def test_media_dir_sits_beside_the_project():
    assert localmedia.media_dir(Path(r"C:\Songs\Shady Grove\Shady Grove.rpp")) == \
        Path(r"C:\Songs\Shady Grove") / "Audio Files"


def test_default_project_gets_its_own_folder(tmp_path):
    """A bare .rpp in Documents would scatter an Audio Files folder next
    to everything else already in there."""
    suggested = localmedia.default_project_file("Shady Grove", projects_dir=tmp_path)
    assert suggested == tmp_path / "Shady Grove" / "Shady Grove.rpp"


def test_default_project_survives_a_name_a_filesystem_would_reject(tmp_path):
    """The name comes from a folder path the user typed, so it reaches
    here unvalidated."""
    suggested = localmedia.default_project_file('Song: "Take 2"?', projects_dir=tmp_path)
    assert suggested.parent.parent == tmp_path
    assert not set(suggested.name) & set('<>:"/\\|?*')


def test_a_name_of_nothing_but_illegal_characters_still_yields_a_path(tmp_path):
    suggested = localmedia.default_project_file('???', projects_dir=tmp_path)
    assert suggested.stem  # never an empty filename


# ---- the copy itself ---------------------------------------------------

def test_local_copy_lands_beside_the_project_with_the_same_bytes(tmp_path):
    store, audio_file = _store_with_audio(tmp_path)
    project = tmp_path / "local" / "Song.rpp"

    dest = localmedia.local_copy(project, store, audio_file)

    assert dest == project.parent / "Audio Files" / audio_file
    assert dest.read_bytes() == store.resolve_audio_path(audio_file).read_bytes()


def test_local_copy_is_not_repeated_for_a_file_already_there(tmp_path):
    """The store names files after their own content hash, so a file with
    this name IS this file. Re-copying a 90MB stem per pull would be pure
    cost."""
    store, audio_file = _store_with_audio(tmp_path)
    project = tmp_path / "local" / "Song.rpp"
    dest = localmedia.local_copy(project, store, audio_file)

    dest.write_bytes(b"marked")  # would be overwritten by a second copy
    assert localmedia.local_copy(project, store, audio_file).read_bytes() == b"marked"


def test_local_copy_raises_rather_than_returning_a_path_to_nothing(tmp_path):
    """The backend decides between falling back and skipping; this must
    not make that decision by handing back a plausible-looking path."""
    store = SharedStore(tmp_path / "shared.dawbridge")
    store.ensure_layout()
    with pytest.raises(OSError):
        localmedia.local_copy(tmp_path / "local" / "Song.rpp", store, "nope.wav")


# ---- "is this the same audio?" ----------------------------------------

def test_a_local_copy_counts_as_the_same_audio(tmp_path):
    """Without this, every clip compares unequal to the shared path and
    every publish re-hashes every stem to conclude nothing changed."""
    store, audio_file = _store_with_audio(tmp_path)
    clip = Clip(id="c1", name="Kick", audio_file=audio_file, start_seconds=0.0, length_seconds=1.0)
    local = str(tmp_path / "local" / "Audio Files" / audio_file)

    assert not sync.should_reimport_audio(local, clip, store)


def test_the_shared_original_still_counts_as_the_same_audio(tmp_path):
    """Projects made before local copies existed point here, and they are
    not stale - just in the wrong place."""
    store, audio_file = _store_with_audio(tmp_path)
    clip = Clip(id="c1", name="Kick", audio_file=audio_file, start_seconds=0.0, length_seconds=1.0)

    assert not sync.should_reimport_audio(str(store.resolve_audio_path(audio_file)), clip, store)


def test_a_re_record_is_still_noticed(tmp_path):
    """The failure this whole comparison exists to prevent: replacing the
    audio under a tagged clip used to publish as 'no change at all'."""
    store, audio_file = _store_with_audio(tmp_path)
    clip = Clip(id="c1", name="Kick", audio_file=audio_file, start_seconds=0.0, length_seconds=1.0)

    assert sync.should_reimport_audio(str(tmp_path / "takes" / "kick-002.wav"), clip, store)


def test_a_file_the_user_named_gets_no_trust_from_matching(tmp_path):
    """Name equality is only a content claim for names the store minted -
    the 16-hex prefix is the hash. A hand-named file could hold anything,
    and trusting it would drop a re-record on the floor."""
    clip = Clip(id="c1", name="Kick", audio_file="kick.wav", start_seconds=0.0, length_seconds=1.0)
    store, _ = _store_with_audio(tmp_path)

    assert not localmedia.is_managed_copy_of(r"C:\anywhere\kick.wav", "kick.wav")
    assert sync.should_reimport_audio(r"C:\anywhere\kick.wav", clip, store)


def test_a_pro_tools_slice_is_not_mistaken_for_the_whole_file(tmp_path):
    """Pro Tools slices to a clip's own boundaries and the slice gets its
    own name, so it must keep failing the shortcut."""
    store, audio_file = _store_with_audio(tmp_path)
    sliced = f"{Path(audio_file).stem}_o1.500_l2.000.wav"

    assert not localmedia.is_managed_copy_of(rf"C:\Session\Audio Files\{sliced}", audio_file)

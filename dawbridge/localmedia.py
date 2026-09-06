"""Keep the DAW's audio on a local disk, not on the shared cloud folder.

Pro Tools already worked this way, for a reason that had nothing to do
with performance: `AddAudio` imports *by reference* and
`rename_target_clip` defaults to rewriting the file on disk, so a session
pointed straight at the shared folder renamed and rewrote the header of a
real shared file (confirmed live). Its fix - copy into the session's own
`Audio Files` folder and import the copy - is what this module
generalises so Reaper can do the same.

Reaper's version of the problem is different but not smaller. It handed
the Google Drive path to `PCM_Source_CreateFromFile` and left it there
permanently, which means:

  - the project breaks if the folder moves, is unshared, or goes offline,
  - Reaper writes `.reapeaks` files into the shared store, where they
    sync to everyone and belong to no one,
  - Google Drive may re-sync a file underneath a DAW that has it open,
  - a cloud-only placeholder looks like a file to `exists()` and like
    silence to the DAW,
  - playback streams from a directory a background daemon is writing to.

**The reason matters, because a wrong one invites the wrong fix.** Audio
read from Google Drive is not degraded - the bytes are identical, and nothing
here should ever touch bit depth, sample rate or format to "improve" it.
The problems are the five above: availability and interference, not
fidelity.

The copy is cheap to keep honest because the shared store already names
every file `<16 hex of content sha1>_<original name>`. A local file
carrying that same name therefore has that same content, so "do I already
have this?" is a filename comparison rather than a re-hash of a 90MB
stem - see `is_managed_copy_of`.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

# Pro Tools' own convention, reused for Reaper so the two DAWs put media
# in the same place relative to the project and nobody has to learn two
# layouts. Reaper has no strong opinion here - a project folder is
# whatever the user saved into.
MEDIA_DIR_NAME = "Audio Files"

# Where a project goes when the user has never saved one and DAWBridge
# has to suggest somewhere. Documents rather than the shared folder:
# putting the project back on Google Drive would reintroduce every problem
# above, and putting it next to the .exe would hide it.
DEFAULT_PROJECTS_DIR = Path.home() / "Documents" / "DAWBridge"

# A name the shared store minted (store.import_audio_file). Matching this
# is what makes a filename comparison a content comparison.
_MANAGED_NAME_RE = re.compile(r"^[0-9a-f]{16}_")

# Characters Windows refuses in a path. The project name comes from a
# folder name so it is usually already legal, but it reaches here through
# the shared-folder path, which a user typed.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def media_dir(project_file: Path) -> Path:
    """Where this project's audio lives: `<project folder>/Audio Files`."""
    return Path(project_file).parent / MEDIA_DIR_NAME


def safe_name(name: str) -> str:
    """A project name reduced to something a filesystem will accept."""
    cleaned = _ILLEGAL.sub("_", str(name)).strip(" .")
    return cleaned or "DAWBridge Project"


def default_project_file(project_name: str, extension: str = ".rpp",
                         projects_dir: Path | None = None) -> Path:
    """A suggested path for a project that has never been saved.

    Its own folder, named after the project, because the audio is about
    to land beside it - dropping a bare `.rpp` into Documents would
    scatter an `Audio Files` folder next to everything else in there.
    """
    stem = safe_name(project_name)
    root = Path(projects_dir) if projects_dir is not None else DEFAULT_PROJECTS_DIR
    return root / stem / f"{stem}{extension}"


def is_managed_copy_of(path, audio_file: str) -> bool:
    """Whether `path` is a local copy of the shared store's `audio_file`.

    True only when the name is one the store minted, so the 16-hex
    content hash in it is doing the work. A file the user named
    themselves gets no such trust: it could hold anything, and treating
    it as identical would silently drop a re-record on the floor.
    """
    if not path or not audio_file:
        return False
    if not _MANAGED_NAME_RE.match(str(audio_file)):
        return False
    try:
        return Path(path).name == str(audio_file)
    except (OSError, ValueError):
        return False


def local_copy(project_file, store, audio_file: str) -> Path:
    """The local path a clip should play, copying it in if it isn't there.

    Returns the destination whether or not the copy happened, so callers
    can point the DAW at it either way; a caller that needs to know the
    file is really there should check `exists()` on the result, which is
    a stat and so safe against cloud-only placeholders.

    Copy failures are deliberately *not* swallowed here. Somewhere up the
    chain has to decide between "use the shared file anyway" and "skip
    this clip", and that decision belongs to the backend, which knows
    which of those leaves the user with a working session.
    """
    source = Path(store.resolve_audio_path(audio_file))
    dest = media_dir(project_file) / Path(audio_file).name
    if dest.exists():
        # Named after its own content hash, so an existing file with this
        # name IS this file. Re-copying would only cost the transfer.
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return dest


def describe_media_home(project_file) -> str:
    """One line for the log saying where the audio is going."""
    return str(media_dir(project_file))

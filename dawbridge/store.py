"""Reads and writes the canonical session on the shared network folder.

Shared folder layout:

    <root>/
      session.json      canonical Session, see model.py
      audio/            audio files referenced by Clip.audio_file
      archive/          previous versions of session.json
        session.json.bak     the immediately-prior version
        session.rNNNN.json   the last _ARCHIVE_KEEP revisions, by number
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from .model import Session

_HASH_CHUNK = 1024 * 1024

# How many past revisions of session.json to keep. Each is a few KB of
# JSON - the audio it references lives in audio/ and is shared, so depth
# here is close to free. 20 covers a long working session of back-and-forth
# without anyone having to think about it.
_ARCHIVE_KEEP = 20

_ARCHIVE_RE = re.compile(r"^session\.r(\d+)\.json$")


class SharedSessionMoved(RuntimeError):
    """Someone published while this machine was reading its DAW.

    Raised instead of writing, because the write would replace work that
    arrived after the caller last checked - and the person who loses it
    would see nothing at all.
    """

    def __init__(self, expected: int, found: int):
        self.expected = expected
        self.found = found
        super().__init__(
            f"the shared session moved from r{expected} to r{found} while your "
            f"DAW was being read - someone else published in the meantime"
        )


class SharedStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.session_path = self.root / "session.json"
        self.audio_dir = self.root / "audio"
        self.archive_dir = self.root / "archive"

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        if not self.session_path.exists():
            Session().save(self.session_path)

    def load(self) -> Session:
        if not self.session_path.exists():
            return Session()
        return Session.load(self.session_path)

    def save(self, session: Session, updated_by: str,
             expected_revision: int | None = None) -> Session:
        """Write `session` as the new canonical revision.

        `expected_revision` closes the window between deciding it's safe to
        publish and actually publishing. Callers check for drift when they
        load the session, then spend seconds to minutes reading a DAW, and
        only then save - a partner publishing anywhere in that gap passes
        every guard silently. Pass the revision you loaded and this refuses
        rather than overwrites. This is the only place that can make that
        check, because it's the only code that reads the on-disk revision
        immediately before writing over it.
        """
        self.ensure_layout()
        on_disk = _revision_of(self.session_path)
        if expected_revision is not None and on_disk > expected_revision:
            raise SharedSessionMoved(expected_revision, on_disk)
        self._archive_current()
        # Advance from whatever canonical actually says, not from whatever
        # revision this in-memory session was loaded at. Those differ
        # whenever the shared folder moved underneath us - the partner
        # published while this machine had the session open, which on a
        # Dropbox folder is ordinary, not exotic. Bumping from the stale
        # number would write a revision at or below one that already
        # existed, and every "has this moved since I last looked?" check
        # compares revision numbers (syncstate.describe_drift), so the
        # partner's copy would report no change at all.
        session.revision = max(session.revision, on_disk)
        session.bump(updated_by)
        session.save(self.session_path)
        return session

    def _archive_current(self) -> None:
        """Keep the version we're about to overwrite.

        A pull replaces canonical state wholesale with whatever DAW you
        just pulled from (see sync.py's module docstring) - there's no
        merge across different source files/projects. That's a deliberate
        simplification, not an oversight, but it means one command can
        blow away real work, and the person losing it never sees it
        happen. This archive is the only thing standing behind that.

        It keeps a run of revisions rather than just the previous one,
        because the failure mode is deeper than one save. Publishing over
        a partner's work takes two saves to become unrecoverable with a
        single backup - theirs, then yours, then any third save and
        theirs is gone. The guards in cli/gui warn before the first one;
        this covers the case where someone clicks through the warning and
        only realises later.
        """
        if not self.session_path.exists():
            return
        self.archive_dir.mkdir(parents=True, exist_ok=True)

        # "The one right before this", for a human who doesn't want to
        # think about revision numbers.
        shutil.copy2(self.session_path, self.archive_dir / "session.json.bak")

        dest = self.archive_dir / f"session.r{_revision_of(self.session_path):04d}.json"
        # A revision only ever advances (Session.bump), so a file already
        # sitting at this revision holds exactly this content.
        if not dest.exists():
            shutil.copy2(self.session_path, dest)
        self._prune_archive()

    def _prune_archive(self, keep: int = _ARCHIVE_KEEP) -> None:
        for _revision, path in self.list_archive()[keep:]:
            try:
                path.unlink()
            except OSError:
                pass  # a full archive must never break a save

    def list_archive(self) -> list[tuple[int, Path]]:
        """Archived revisions as (revision, path), newest first."""
        if not self.archive_dir.is_dir():
            return []
        found = []
        for path in self.archive_dir.iterdir():
            match = _ARCHIVE_RE.match(path.name)
            if match:
                found.append((int(match.group(1)), path))
        found.sort(key=lambda pair: pair[0], reverse=True)
        return found

    def restore_archived(self, revision: int, updated_by: str) -> Session:
        """Republish an archived revision as the current shared session.

        Deliberately goes *forward*: the restored content is saved as a
        new revision rather than rewinding the counter. Everything that
        asks "has this moved since I last looked?" (syncstate.describe_drift)
        compares revision numbers, so a rewind would read as "nothing
        happened" on the other machine and the restore would be invisible
        to the person it matters most to. Restoring also archives whatever
        it replaces, so an unwanted restore is itself undoable.
        """
        match = next((path for rev, path in self.list_archive() if rev == revision), None)
        if match is None:
            raise FileNotFoundError(
                f"no archived revision r{revision} in {self.archive_dir}"
            )
        return self.save(Session.load(match), updated_by=updated_by)

    def import_audio_file(self, local_path: Path) -> str:
        """Copy a local audio file into the shared audio/ dir, deduped by
        content hash, and return its path relative to audio/ for use as
        Clip.audio_file.
        """
        self.ensure_layout()
        local_path = Path(local_path)
        digest = _hash_file(local_path)
        # Strip any hash prefix this file already carries. A file that has
        # round-tripped (shared folder -> DAW -> pulled back) arrives
        # already named "<hash>_original.wav", and blindly prefixing again
        # produced "<hash2>_<hash1>_original.wav", growing a prefix per
        # round trip and defeating dedup - confirmed live, the shared
        # folder accumulated multiple ~60MB copies of identical audio.
        base_name = _strip_hash_prefix(local_path.name)
        dest_name = f"{digest[:16]}_{base_name}"
        dest_path = self.audio_dir / dest_name
        if not dest_path.exists():
            shutil.copy2(local_path, dest_path)
        return dest_name

    def resolve_audio_path(self, audio_file: str) -> Path:
        return self.audio_dir / audio_file


_HASH_PREFIX_RE = re.compile(r"^[0-9a-f]{16}_")


def _strip_hash_prefix(name: str) -> str:
    """Remove a leading "<16 hex chars>_" that a previous import added, so
    repeated round trips don't stack prefixes. Applied repeatedly in case
    a file already accumulated several before this was fixed.
    """
    while _HASH_PREFIX_RE.match(name):
        name = _HASH_PREFIX_RE.sub("", name, count=1)
    return name


def _revision_of(path: Path) -> int:
    """The revision recorded in a session.json, or 0 if it can't be read.

    Falling back to 0 rather than raising keeps a corrupt or half-written
    canonical file from blocking the save that might be replacing it -
    and session.json.bak still holds the bytes either way.
    """
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("revision", 0))
    except Exception:
        return 0


def _hash_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()

"""Is this shared folder OK?

One implementation, two front ends. `dawbridge doctor` and the GUI's
"Check folder" button both call `check_folder`, so they cannot drift into
disagreeing about whether a folder is healthy - which would be its own
small disaster, since the whole point is to be believed.

Everything here is read-only and **metadata-only**. The folder lives in
Google Drive, where a file can be a cloud placeholder and reading it forces a
download. Existence and size, never contents. A recursive read over this
folder once began hydrating the user's entire account.
"""
from __future__ import annotations

from pathlib import Path

from . import conflicts
from .model import Session
from .sync import find_overlapping_clips


def check_folder(store) -> tuple[list[str], list[tuple[str, int]]]:
    """Return (problems, orphaned_audio).

    `problems` are things a human should act on, worst first. Orphaned
    audio is returned separately because it is *not* a fault - archived
    revisions legitimately reference files the current session doesn't -
    and reporting it as one would train people to ignore the list.
    """
    session = store.load()
    problems: list[str] = []

    # A conflicted copy is the one failure revision numbers physically
    # cannot express: both machines advance from the same revision to the
    # same number, so drift reads as zero. It has to be found by name.
    problems.extend(conflicts.describe_conflicts(store.root))

    referenced = {c.audio_file for t in session.tracks for c in t.clips if c.audio_file}
    missing = sorted(f for f in referenced if not store.resolve_audio_path(f).exists())
    for name in missing:
        owners = [f"{t.name}/{c.name}" for t in session.tracks
                  for c in t.clips if c.audio_file == name]
        problems.append(f"audio missing from the shared folder: {name} "
                        f"(used by {', '.join(owners[:3])})")
    if missing:
        # Said every time, because the likeliest cause is the least
        # alarming one and people assume the worst about missing audio.
        problems.append("missing audio can also just mean Google Drive hasn't finished "
                        "syncing - check the sync icon before assuming it's lost")

    for track in session.tracks:
        for a, b in find_overlapping_clips(track.clips) if track.clips else []:
            problems.append(f"clips overlap on {track.name!r}: {a.name} and {b.name}")

    silent = [c.name for t in session.tracks for c in t.clips if not c.audio_file]
    if silent:
        problems.append(f"{len(silent)} clip(s) reference no audio at all: "
                        f"{', '.join(silent[:4])}")

    return problems, _orphaned_audio(store, referenced)


def _orphaned_audio(store, referenced: set[str]) -> list[tuple[str, int]]:
    """Audio that nothing points at - not the current session, and not any
    archived revision. Checking the archive matters: without it, every
    file kept solely so an old revision stays restorable would be listed
    as junk, and deleting it would hollow out the restore.
    """
    kept = set(referenced)
    for _revision, path in store.list_archive():
        try:
            kept |= {c.audio_file for t in Session.load(path).tracks
                     for c in t.clips if c.audio_file}
        except Exception:
            pass  # an unreadable archived revision must not hide live files
    if not store.audio_dir.is_dir():
        return []
    on_disk = {p.name: p.stat().st_size for p in store.audio_dir.iterdir() if p.is_file()}
    return sorted(((name, size) for name, size in on_disk.items() if name not in kept),
                  key=lambda pair: -pair[1])


def describe_revision(revision: int, path: Path) -> str:
    """One archived revision as a line someone can choose from."""
    try:
        session = Session.load(path)
    except Exception:
        return f"r{revision}  (unreadable)"
    when = (session.updated_at or "?").replace("T", " ")[:16]
    clips = sum(len(t.clips) for t in session.tracks)
    return (f"r{revision:<4d} {when:<17s} {session.updated_by or '?':<18s} "
            f"{len(session.tracks)} track(s), {clips} clip(s)")

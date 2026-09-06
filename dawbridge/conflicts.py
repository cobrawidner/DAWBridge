"""Detects the files a syncing service leaves behind when two people write
the same file at once.

No consumer sync service merges, and none of them ask. When two machines
write session.json at nearly the same moment, one write keeps the name
and the other is kept under a different one. Dropbox names it:

    session (Conner's conflicted copy 2026-08-09).json

Nothing else in DAWBridge has ever looked for that file, and the
consequences are worse than "a stray file appeared":

  - The loser's entire publish is in it. It is not in archive/, because
    archive/ only ever holds versions that SharedStore.save() itself
    replaced - this one was never on disk under the real name, so
    `dawbridge history` and `dawbridge restore` cannot bring it back.
  - Both machines wrote the SAME revision number (they advanced from the
    same starting revision), so syncstate.describe_drift - the one check
    that asks "has canonical moved since I last looked?" - computes a
    delta of zero and stays silent. The publish guard is not just absent
    here, it is actively defeated: the losing machine's recorded revision
    now matches canonical's, so its next publish sails through too.

Confirmed by test (tests/test_conflicts.py) using the real store.

So detection has to be by filename, and it has to be loud. Everything
here is stat/listing only - no recursion and no reading of audio, because
these files live in a synced folder where reading bytes forces a
download.

**Two detectors, deliberately.** The name-pattern one below knows
Dropbox's wording. The project moved to Google Drive in September 2026,
and Drive's conflict naming is NOT something this file knows - so a
detector that only matched Dropbox's phrasing would have silently
stopped working at exactly the moment nobody noticed, on the one check
that catches a lost publish. `find_stray_sessions` is the answer: in the
shared root there should be exactly one file called session.json and
nothing else matching session*.json, whatever a sync service decided to
call its copy. It needs no knowledge of any service's conventions and
cannot go quietly out of date.
"""
from __future__ import annotations

import re
from pathlib import Path

#: Dropbox's wording, kept because it is precise where it applies and
#: some collaborators may still be on Dropbox. It is NOT sufficient on
#: its own - see find_stray_sessions.
#: Dropbox has used several wordings over the years ("conflicted copy",
#: "'s conflicted copy", with and without a date) and adds a separate
#: "(Case Conflict)" for names differing only in case. Match the phrase
#: anywhere inside a trailing parenthetical rather than pinning the exact
#: sentence, so a wording change doesn't silently turn detection off.
_CONFLICT_RE = re.compile(r"\([^)]*(?:conflicted copy|case conflict)[^)]*\)", re.IGNORECASE)

#: Only ever look in these, and never recursively. audio/ can hold
#: gigabytes and a peaks/ subdirectory; listing names is free, walking
#: into cloud-backed subtrees is not.
_SCAN_DIRS = ("", "audio", "archive")


def is_conflicted_name(name: str) -> bool:
    """Whether a filename looks like a sync service's conflicted copy."""
    return bool(_CONFLICT_RE.search(name))


def original_name(name: str) -> str:
    """The name a conflicted copy was made from.

    "session (Conner's conflicted copy 2026-08-09).json" -> "session.json"
    """
    stripped = _CONFLICT_RE.sub("", name)
    # Removing the parenthetical leaves the space that preceded it.
    return re.sub(r"\s+(?=\.[^.]*$)", "", stripped).strip()


def find_conflicts(root: Path) -> list[Path]:
    """Every conflicted-copy file in the shared folder, newest name first.

    Listing only - nothing here opens a file, so this is safe to call on
    a synced folder full of cloud-only audio.
    """
    found: list[Path] = []
    for sub in _SCAN_DIRS:
        directory = Path(root) / sub if sub else Path(root)
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        found.extend(p for p in entries if p.is_file() and is_conflicted_name(p.name))
    return sorted(found)


def find_stray_sessions(root: Path) -> list[Path]:
    """Files in the shared root that look like another copy of session.json.

    The shared root holds exactly one session.json, plus notify.json and
    the audio/ and archive/ directories. Anything else matching
    `session*.json` there is a second copy of the canonical session that
    nothing is reading - which is what a lost publish looks like on disk.

    Named-pattern free on purpose. Dropbox writes "(conflicted copy)";
    Google Drive does something else; the next service will do a third
    thing. This asks the question that actually matters - "is there more
    than one session file here?" - so it keeps working when the wording
    changes or the whole service does.

    Root only, never archive/, which is *supposed* to be full of
    session.rNNNN.json.
    """
    try:
        entries = list(Path(root).iterdir())
    except OSError:
        return []
    return sorted(
        p for p in entries
        if p.is_file()
        and p.name.lower() != "session.json"
        and p.name.lower().startswith("session")
        and p.suffix.lower() == ".json"
    )


def find_session_conflicts(root: Path) -> list[Path]:
    """Conflicted copies of session.json specifically - the ones that mean
    somebody's publish was lost rather than just a duplicated file.
    """
    named = [p for p in find_conflicts(root) if original_name(p.name).lower() == "session.json"]
    # Union, not either/or: a Dropbox-named copy also matches
    # session*.json, and dropping one detector for the other would lose
    # the case each is better at.
    return sorted(set(named) | set(find_stray_sessions(root)))


def describe_session_conflict(root: Path) -> str | None:
    """One paragraph for a human whose partner's publish is sitting in a
    conflicted copy, or None when there's nothing to say.

    Deliberately quiet when the conflicted copy is byte-identical to the
    live session.json: Dropbox does sometimes conflict two writes of the
    same content, and nothing was lost in that case. A warning nobody can
    act on is a warning people learn to skim.
    """
    root = Path(root)
    conflicts = find_session_conflicts(root)
    if not conflicts:
        return None

    session_path = root / "session.json"
    live_bytes = _read_small(session_path)

    interesting = [p for p in conflicts if _read_small(p) != live_bytes or live_bytes is None]
    if not interesting:
        return None

    lines = [
        f"{len(interesting)} unmerged publish(es) are sitting in this folder - "
        f"your sync service could not merge two people publishing at once, so it kept one "
        f"and set the other aside under a different name. DAWBridge only ever reads "
        f"session.json, so that work is invisible to both of you:"
    ]
    for path in interesting:
        lines.append(f"  {path.name} - {_describe_session_file(path)}")
    lines.append(f"  session.json (what everyone sees) - {_describe_session_file(session_path)}")
    lines.append(
        "This is NOT in archive/, so `dawbridge history` and `dawbridge restore` cannot "
        "recover it - archive/ only holds versions DAWBridge itself replaced. To keep the "
        "set-aside version instead, rename it over session.json (copy the current one "
        "somewhere first). To discard it, delete it. Either way, decide before publishing "
        "again."
    )
    return "\n".join(lines)


def describe_conflicts(root: Path) -> list[str]:
    """Every conflicted copy in the folder as human-readable lines - the
    session ones first and at length, then anything else (audio, archive)
    as a one-liner each.

    Not wired into any command yet; see the report accompanying this
    change for the `dawbridge doctor` surface it's meant for.
    """
    lines: list[str] = []
    session_note = describe_session_conflict(root)
    if session_note:
        lines.append(session_note)
    for path in find_conflicts(Path(root)):
        if original_name(path.name).lower() == "session.json":
            continue
        lines.append(
            f"{path.name} is a conflicted copy of {original_name(path.name)!r} - two machines "
            f"wrote it at once. Nothing references it; delete it once you've checked you don't "
            f"want it."
        )
    return lines


def _read_small(path: Path) -> bytes | None:
    """Bytes of a small JSON file, or None if it can't be read.

    Only ever called on session.json-sized files. Never on audio.
    """
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _describe_session_file(path: Path) -> str:
    """"r36 by conner@protools, 7 track(s), 2026-08-09T16:29:41+00:00"."""
    import json

    raw = _read_small(path)
    if raw is None:
        return "unreadable"
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return "not valid session JSON"
    return (
        f"r{data.get('revision', '?')} by {data.get('updated_by') or '?'}, "
        f"{len(data.get('tracks') or [])} track(s), {data.get('updated_at') or 'unknown time'}"
    )

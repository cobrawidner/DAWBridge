"""Per-machine record of which canonical revision this machine last saw.

Deliberately NOT stored in the shared folder: the whole point is to answer
"has the *other* person changed things since I last looked?", which needs a
local, per-machine answer. Two people sharing one file would overwrite each
other's notion of "last seen" and the question becomes unanswerable.

Keyed by (shared folder, daw) because one machine can drive both DAWs
against the same folder and each side syncs independently.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_STATE_PATH = Path.home() / ".dawbridge_state.json"


def _load() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _key(folder: Path, daw: str) -> str:
    return f"{Path(folder).resolve()}|{daw}"


def record_sync(folder: Path, daw: str, revision: int, action: str, project: str = "") -> None:
    """Remember that this machine saw `revision` for this folder/DAW, and
    which local project it was working from.

    An unknown project (`project=""`) means "this caller didn't ask",
    never "there is no project". Dropping the previously recorded one
    would switch describe_project_change off permanently, and it's the
    guard against a pull from the wrong project replacing the whole
    shared session - the failure that silently doubled every track during
    development. The GUI calls this without a project on every sync, so
    forgetting here is not a hypothetical.
    """
    data = _load()
    key = _key(folder, daw)
    entry = {"revision": revision, "action": action}
    previous_project = (data.get(key) or {}).get("project")
    if project:
        entry["project"] = project
    elif previous_project:
        entry["project"] = previous_project
    data[key] = entry
    try:
        _STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass  # a missing breadcrumb must never break a sync


def describe_project_change(folder: Path, daw: str, project: str) -> Optional[str]:
    """One line when this DAW has a different project open than it did
    last sync, else None.

    Worth its own check because a pull REPLACES the shared session with
    whatever's open. Syncing from a different project isn't an edit, it's
    a swap - during development exactly this silently doubled every track
    in the shared session, and nothing in the output hinted at why.
    """
    seen = last_synced(folder, daw)
    if not seen or not project:
        return None
    previous = seen.get("project")
    if not previous or Path(previous) == Path(project):
        return None
    return (
        f"this {daw} has a different project open than your last sync - "
        f"was {Path(previous).name!r}, now {Path(project).name!r}"
    )


def last_synced(folder: Path, daw: str) -> Optional[dict]:
    """{"revision": int, "action": "pull"|"push"} or None if never synced."""
    return _load().get(_key(folder, daw))


def describe_drift(folder: Path, daw: str, current_revision: int) -> Optional[str]:
    """One line on how far canonical has moved since this machine last
    synced, or None when there's nothing worth saying.

    A conflicted copy of session.json is reported here, ahead of and
    regardless of the revision arithmetic, because it is drift the
    revision number structurally cannot show: both machines advanced from
    the same revision, so both wrote the same number and the subtraction
    below yields zero. Every caller (CLI pull/preview/push, GUI
    pull/preview) already routes through this function, which is why the
    check lives here rather than in a new command nobody runs. See
    conflicts.py.
    """
    from . import conflicts

    conflict = conflicts.describe_session_conflict(Path(folder))

    seen = last_synced(folder, daw)
    if seen is None:
        return _join(conflict, "this machine has never synced with this folder before")
    delta = current_revision - int(seen.get("revision", 0))
    if delta <= 0:
        return conflict
    revisions = "revision" if delta == 1 else "revisions"
    # Deliberately states the fact only. Callers say what it means for
    # what they're about to do - the same drift is reassuring before a
    # load ("you'll get their work") and alarming before a publish
    # ("you'll erase it").
    return _join(
        conflict,
        f"someone else has published since your last {seen.get('action', 'sync')} - "
        f"you last saw r{seen.get('revision')}, shared session is now r{current_revision} "
        f"({delta} {revisions} ahead)",
    )


def _join(*parts: Optional[str]) -> Optional[str]:
    present = [p for p in parts if p]
    return "\n".join(present) if present else None

"""Canonical session representation shared between Reaper and Pro Tools.

This is the schema that lives in the shared-folder session.json. Both DAW
backends read and write *this*, never each other's native project file.

Deliberately excluded from this schema (see feasibility notes / README):
  - Plugin/insert/send state - each side owns their own effects chain
    locally and it is never touched by a sync.
  - Automation curves - not currently exposed for writing via PTSL, so
    it can't be round-tripped to Pro Tools yet. Modeled as absent rather
    than half-supported.

Track.muted IS synced (unlike the mix/plugin state above) - it's part of
the arrangement (which tracks are meant to be heard), not mix tuning, and
staying silent about it across a bridge is exactly the kind of thing that
causes confusion between two people passing a project back and forth.
Every pull captures the live mute state; every push applies it,
overwriting whatever was there - there's no merge, same as everything
else in this schema (see sync.py's module docstring).

Every Track and Clip carries a short, stable `id` that gets embedded in the
DAW-native object's name (see tagging.py) so a future pull/push can find it
again even if the human-readable name changes.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA_VERSION = 1


class UnsupportedSchemaVersion(Exception):
    """The shared session was written by a newer DAWBridge than this one.

    Two people, two machines, one shared folder, and nothing that updates
    them together - so one of them WILL open a session written by a
    version the other hasn't installed yet. That has to end in a sentence
    they can act on, not a traceback.
    """


def new_id() -> str:
    """Short stable id used for cross-DAW matching (see tagging.py)."""
    return uuid.uuid4().hex[:8]


def _split_known(cls, data: dict) -> tuple[dict, dict]:
    """Split a decoded JSON object into (fields this version knows about,
    everything else).

    The "everything else" half is the point. A pull REPLACES the shared
    session, so an out-of-date client that drops a field it doesn't
    understand doesn't just fail to read it - it deletes the other
    person's data from the shared folder on the next publish, silently
    and permanently. Keeping the unknowns and writing them back out means
    the old client passes them through untouched.
    """
    known = {f.name for f in fields(cls)} - {"extra"}
    kept = {k: v for k, v in data.items() if k in known}
    extra = {k: v for k, v in data.items() if k not in known}
    return kept, extra


def _wire(obj):
    """Dataclass -> JSON-ready dict, with `extra` merged back in flat.

    Deliberately not `dataclasses.asdict`: that would emit the carrier
    field itself as a literal "extra" key, which is both ugly on the wire
    and a new unknown key for every other client to puzzle over.
    """
    if is_dataclass(obj):
        out = {}
        for f in fields(obj):
            if f.name == "extra":
                continue
            out[f.name] = _wire(getattr(obj, f.name))
        out.update(getattr(obj, "extra", None) or {})
        return out
    if isinstance(obj, list):
        return [_wire(item) for item in obj]
    return obj


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Clip:
    id: str
    name: str
    audio_file: str  # path relative to the shared folder's audio/ directory
    start_seconds: float
    length_seconds: float
    source_offset_seconds: float = 0.0
    fade_in_seconds: float = 0.0
    fade_out_seconds: float = 0.0
    # True when the clip repeats its source to fill length_seconds (Reaper's
    # "loop source"). Pro Tools has no equivalent, so its backend warns and
    # places a single iteration rather than silently flattening the loop.
    loop_source: bool = False
    #: Fields written by a newer DAWBridge that this version doesn't know
    #: about, carried through untouched - see _split_known.
    extra: dict = field(default_factory=dict, repr=False, compare=False)

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.length_seconds

    @classmethod
    def new(cls, **kwargs) -> "Clip":
        return cls(id=new_id(), **kwargs)

    @classmethod
    def from_dict(cls, data: dict) -> "Clip":
        known, extra = _split_known(cls, data)
        return cls(**known, extra=extra)


@dataclass
class Track:
    id: str
    name: str
    kind: str = "audio"  # "audio" | "midi" (midi unimplemented in v1 backends)
    order: int = 0
    channels: int = 2  # channel width (1 = mono, 2 = stereo); must match on both sides
    muted: bool = False  # synced every pull/push - see model.py docstring
    #: Track colour as a neutral "#RRGGBB", or None for "no colour set".
    #: Neither DAW speaks this natively and they don't agree with each
    #: other - Reaper stores a BGR-packed OS integer, Pro Tools can only
    #: be SET to one of 69 palette entries - so the colour that comes
    #: back from a round trip is a near match, not the same number. That
    #: was accepted deliberately; see color.py for what each side really
    #: stores and for the guard that stops the approximation compounding.
    color: Optional[str] = None
    clips: list[Clip] = field(default_factory=list)
    extra: dict = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def new(cls, **kwargs) -> "Track":
        return cls(id=new_id(), **kwargs)

    @classmethod
    def from_dict(cls, data: dict) -> "Track":
        known, extra = _split_known(cls, data)
        known["clips"] = [Clip.from_dict(c) for c in data.get("clips", [])]
        return cls(**known, extra=extra)

    def clip_by_id(self, clip_id: str) -> Optional[Clip]:
        return next((c for c in self.clips if c.id == clip_id), None)


@dataclass
class Marker:
    id: str
    name: str
    time_seconds: float
    extra: dict = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def new(cls, **kwargs) -> "Marker":
        return cls(id=new_id(), **kwargs)

    @classmethod
    def from_dict(cls, data: dict) -> "Marker":
        known, extra = _split_known(cls, data)
        return cls(**known, extra=extra)


@dataclass
class Session:
    schema_version: int = SCHEMA_VERSION
    name: str = "Untitled"
    sample_rate: int = 48000
    # Session tempo/meter. Carried so the grid matches on both sides, but
    # PTSL exposes NO tempo or meter command at all (verified against the
    # 2025 command table), so the Pro Tools backend can only report these
    # for the user to set by hand - see protools_backend's push warnings.
    tempo_bpm: float = 120.0
    time_signature_numerator: int = 4
    time_signature_denominator: int = 4
    tracks: list[Track] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    revision: int = 0
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None
    extra: dict = field(default_factory=dict, repr=False, compare=False)

    def track_by_id(self, track_id: str) -> Optional[Track]:
        return next((t for t in self.tracks if t.id == track_id), None)

    def bump(self, updated_by: str) -> None:
        self.revision += 1
        self.updated_by = updated_by
        self.updated_at = _now_iso()

    def to_json(self) -> str:
        return json.dumps(_wire(self), indent=2, sort_keys=False)

    @classmethod
    def from_json(cls, text: str) -> "Session":
        data = json.loads(text)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        # A bumped schema version means a change this build can't be
        # trusted to understand, so refuse - but refuse in a sentence,
        # because the person reading it is a musician looking at a shared
        # folder, not a stack trace. An UNKNOWN FIELD at the same version
        # is the opposite case and is carried through silently: that's
        # how a newer client can add things without breaking an older
        # one. See _split_known.
        found_version = data.get("schema_version", SCHEMA_VERSION)
        try:
            too_new = int(found_version) > SCHEMA_VERSION
        except (TypeError, ValueError):
            too_new = True
        if too_new:
            raise UnsupportedSchemaVersion(
                f"This shared session was saved by a newer version of DAWBridge "
                f"(session format v{found_version}; this copy understands v{SCHEMA_VERSION}). "
                f"Update DAWBridge on this machine to open it - nothing has been changed."
            )

        known, extra = _split_known(cls, data)
        known["tracks"] = [Track.from_dict(t) for t in data.get("tracks", [])]
        known["markers"] = [Marker.from_dict(m) for m in data.get("markers", [])]
        return cls(**known, extra=extra)

    @classmethod
    def load(cls, path: Path) -> "Session":
        return cls.from_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        # Atomic-ish write: temp file then rename, so a crash mid-write
        # never leaves a corrupt session.json on the shared folder.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        tmp.replace(path)

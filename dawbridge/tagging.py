"""Embed/parse a stable bridge id in a DAW-native track or clip name.

Neither Reaper nor Pro Tools exposes a cross-DAW-friendly persistent id we
can rely on, so DAWBridge stamps its own short id into the *name* of every
track and clip it manages, e.g.:

    "Lead Vocal #a1b2c3d4"

On the next pull/push, a backend parses this tag back out to recognize
"this is the same object as canonical track a1b2c3d4" even if the
human-readable part of the name has since changed. Untagged local
objects are treated as local-only content the bridge has never seen -
they're left alone, never claimed or overwritten.
"""
from __future__ import annotations

import re
from typing import Optional

_TAG_RE = re.compile(r"^(?P<base>.*?)\s*#(?P<id>[0-9a-f]{8})$")


def tag(base_name: str, obj_id: str) -> str:
    """Compose a DAW-native name carrying the bridge id."""
    base_name = strip_tag(base_name)
    return f"{base_name} #{obj_id}"


def parse_tag(name: str) -> tuple[str, Optional[str]]:
    """Split a DAW-native name into (base_name, bridge_id | None)."""
    match = _TAG_RE.match(name.strip())
    if match:
        return match.group("base"), match.group("id")
    return name, None


def strip_tag(name: str) -> str:
    base, _ = parse_tag(name)
    return base

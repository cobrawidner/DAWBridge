"""Track colour, and the two DAWs' incompatible ideas of what one is.

Canonical stores a neutral `#RRGGBB` string (or None for "no colour"),
and each backend converts. Neither DAW speaks that format natively, and
they don't agree with each other either. What was actually measured,
live, on 2026-08-11 - none of this is from documentation:

**Reaper** (7.78, Windows, through reapy)
  - `ColorToNative(r, g, b)` packs to `0x00BBGGRR` on Windows - BGR, not
    RGB. Measured: (255,0,0) -> 0x0000FF, (0,0,255) -> 0xFF0000. That
    packing is per-platform, which is exactly why ReaScript provides
    `ColorToNative`/`ColorFromNative`; the Reaper backend calls those
    rather than shifting bytes here, so a macOS build stays correct.
  - **`I_CUSTOMCOLOR` must not be read directly.** A track nobody has
    ever coloured still holds a non-zero value in its low 24 bits - a
    brand new track read `16576` (0x0040C0) - and only the `0x1000000`
    flag bit says whether a custom colour was actually set. Reading the
    number alone would publish an orange nobody chose for every
    uncoloured track in the project.
  - `GetTrackColor()` collapses that correctly: **0** means no custom
    colour, anything else is `native | 0x1000000`. `SetTrackColor()`
    sets the flag for you. Those are the accessors to use.
  - Persists to the .rpp as `PEAKCOL <flagged int>` (verified in a saved
    scratch project).

**Pro Tools** (2025, PTSL through py-ptsl 601.1.0)
  - Reading is free: the `Track` message already returned by
    `engine.track_list()` carries `color` as an `#AARRGGBB` string
    (e.g. `#ff13355f`, alpha always ff).
  - Writing is **palette-only**. `SetTrackColor` takes a `color_index`
    and nothing else - there is no arbitrary-RGB write. The index is
    1-based into `GetColorPalette(CPTarget_Tracks)` and its valid range
    is [1;69]; 0 and 70 are both rejected outright.

So a colour crossing into Pro Tools is snapped to the nearest of 69, and
that is not fixable - it's what the API offers. Travis accepted the
inexactness ("I think we can live with colors not being exact"). What is
NOT acceptable is *accumulation*: Reaper publishes #3F7FBF, Pro Tools
snaps it, and if Pro Tools then republishes the snapped value over
canonical the original is gone and every round trip nudges it further.
`resolve_captured` is the guard - see its docstring. It is the same
reasoning as `sync._MOVE_TOLERANCE_SECONDS`: compare at the resolution
that actually matters, not exactly.
"""
from __future__ import annotations

import re

#: `#RGB` is not accepted: neither DAW emits it, and guessing that "#abc"
#: means "#aabbcc" would silently invent a colour on malformed input.
_HEX = re.compile(r"^#?(?:[0-9a-fA-F]{2})?([0-9a-fA-F]{6})$")


def normalise(value) -> str | None:
    """Any colour we might be handed, as canonical `#RRGGBB`, or None.

    Accepts `#RRGGBB`, bare `RRGGBB`, and Pro Tools' own `#AARRGGBB`
    (the alpha is dropped - it is always ff, and canonical has no notion
    of a transparent track). Anything else is None rather than an
    exception: a colour is cosmetic, and no reading of one is worth
    failing a publish over.
    """
    if not isinstance(value, str):
        return None
    match = _HEX.match(value.strip())
    if match is None:
        return None
    return "#" + match.group(1).upper()


def rgb(value) -> tuple[int, int, int] | None:
    """`#RRGGBB` -> (r, g, b), or None if it isn't a colour."""
    text = normalise(value)
    if text is None:
        return None
    return int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16)


def to_hex(r: int, g: int, b: int) -> str:
    """(r, g, b) -> `#RRGGBB`, clamping anything out of range."""
    def clamp(v):
        return max(0, min(255, int(v)))
    return "#{:02X}{:02X}{:02X}".format(clamp(r), clamp(g), clamp(b))


def same(a, b) -> bool:
    """Whether two colours are the same colour, None-safe.

    Compares normalised, so `#3f7fbf`, `#3F7FBF` and Pro Tools'
    `#ff3f7fbf` are one colour rather than three.
    """
    return normalise(a) == normalise(b)


def _distance_squared(one: tuple[int, int, int], other: tuple[int, int, int]) -> float:
    """"How different do these look", not "how far apart are the numbers".

    Plain Euclidean distance in RGB gets hues visibly wrong - it will
    happily answer a muddy green for a bright red. This is the standard
    "redmean" weighting, which is cheap, has no lookup tables, and is
    close enough that the nearest palette entry is recognisably the same
    colour family. That is the entire point of the feature: colour is how
    a musician says "these four are the drums".
    """
    r1, g1, b1 = one
    r2, g2, b2 = other
    rmean = (r1 + r2) / 2
    dr, dg, db = r1 - r2, g1 - g2, b1 - b2
    return (2 + rmean / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rmean) / 256) * db * db


def nearest(value, palette) -> str | None:
    """The palette entry closest to `value`, as `#RRGGBB`.

    None when there's no colour to match or no palette to match it
    against - callers treat that as "this DAW can't show a colour right
    now" and leave things alone, rather than picking arbitrarily.
    """
    target = rgb(value)
    if target is None:
        return None
    best, best_distance = None, None
    for entry in palette:
        candidate = rgb(entry)
        if candidate is None:
            continue
        distance = _distance_squared(target, candidate)
        if best_distance is None or distance < best_distance:
            best, best_distance = normalise(entry), distance
    return best


def resolve_captured(previous, live, quantise=None) -> str | None:
    """What a pull should record as a track's colour. The drift guard.

    `previous` is what canonical already says, `live` is what the DAW is
    showing right now, and `quantise` maps a colour to what THIS DAW is
    capable of displaying (nearest palette entry for Pro Tools; identity
    for Reaper, which can show anything).

    The rule: **if canonical's colour maps to what the DAW is showing,
    nothing changed.** Pro Tools cannot display #3F7FBF - it shows the
    nearest of 69 - so reading that snapped colour back and publishing it
    would overwrite the original with an approximation, and the next
    round trip would approximate the approximation. Comparing through
    `quantise` means the snap happens at most once and never compounds.

    `live is None` means "this DAW is not telling us a colour for this
    track" and canonical is left alone. For Reaper that also covers a
    track with no custom colour at all, so a Reaper user cannot clear a
    colour across the bridge - a deliberate trade. Pro Tools has no
    concept of an uncoloured track and always reports one, so a cleared
    colour would come straight back on the partner's next publish anyway,
    as a DIFFERENT colour. Keeping what we have is the quieter wrong
    answer of the two.
    """
    previous = normalise(previous)
    live = normalise(live)
    if live is None:
        return previous
    if previous is None:
        return live
    if quantise is not None and same(quantise(previous), live):
        return previous
    return live


#: Pro Tools' built-in track colour palette, read live from
#: `GetColorPalette(CPTarget_Tracks)` on Pro Tools 2025 (2026-08-11), in
#: `SetTrackColor` index order - entry 1 of 69 is first.
#:
#: The backend NEVER uses this: it asks the running Pro Tools for its own
#: palette every time, so it cannot be wrong. This copy exists only so
#: that `sync.preview_push`, which is DAW-agnostic and has no engine to
#: ask, can predict the snap and avoid reporting a recolour on every
#: single pull for a colour Pro Tools is already showing as close as it
#: can. If a future Pro Tools ships a different palette, the worst this
#: can do is describe a change that then isn't visible - it degrades into
#: a cosmetic inaccuracy about a cosmetic field, never a wrong write.
PROTOOLS_TRACK_PALETTE = (
    "#2C00FC", "#5600FC", "#8800FC", "#BF00FC", "#BE00C0", "#BD0088",
    "#BD0054", "#BC000D", "#BD1E0D", "#BD520E", "#BE8911", "#C0C514",
    "#89C511", "#57C610", "#2EC60F", "#1CC60E", "#1EC654", "#20C488",
    "#23C3C1", "#27C1FD", "#2184FC", "#1C4AFC", "#1900FC", "#1E00A3",
    "#3700A3", "#5500A3", "#7400A4", "#7C0089", "#7B0066", "#7A0046",
    "#7A000B", "#7A120B", "#7A310C", "#7B510D", "#898010", "#66800E",
    "#48800D", "#2D800C", "#18800C", "#158033", "#167F51", "#1A8C7E",
    "#1D8DA4", "#1969A4", "#1646A3", "#1423A3", "#14005F", "#21005F",
    "#31005F", "#41005F", "#4B0057", "#470042", "#470031", "#47000B",
    "#470C0B", "#471C0B", "#472C0C", "#574D0F", "#424A0C", "#324A0C",
    "#234A0C", "#154B0B", "#0F4A1D", "#0F4A2C", "#14594C", "#16595F",
    "#14475F", "#13355F", "#11225F",
)

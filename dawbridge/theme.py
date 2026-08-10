"""The DAWBridge look, in one place.

Everything about how the window is coloured, framed and typeset lives
here; `gui.py` builds widgets and asks this module to dress them. The
direction is early-2000s professional audio hardware - flat blue-grey
faceplate, one-pixel machined frames, small tracked labels, one accent
colour, and status sunk into a dark recessed readout.

Three things are load-bearing and easy to get wrong:

1. **The `clam` base theme is not optional.** Windows' default `vista`
   ttk theme draws native parts and silently ignores most colour
   options, so a fully specified palette still renders as a generic
   Windows app. `clam` draws everything itself and honours the lot.

   The same trap has a second door: the *classic* `tk.Scrollbar` is a
   native Win32 control whatever the ttk theme is, and it silently
   ignores `-background`/`-troughcolor`. One shipped as part of
   `ScrolledText` and rendered a #f0f0f0 silver bar down the side of a
   black readout - the brightest thing in the window. Readouts build
   `tk.Text` + `ttk.Scrollbar` by hand instead; see `SCROLLBAR`.

2. **The readout rule.** Anything that reports state - the status pane,
   the log, LEDs - sits on `DISPLAY` (#101315) and never on the
   faceplate: LED green on mid-grey measures about 2.4:1 and is
   genuinely unreadable, while the same green on the readout clears 7:1.
   The readout stays dark even if a light "brushed aluminium" chassis is
   ever added, because a display is dark whatever colour the case is.
   That is why the `READOUT_*` colours below are separate literals
   rather than aliases of the chassis ink tokens: a chassis token used
   on the readout would go dark-on-dark the moment a light theme exists.

3. **Colour is a signal, not decoration.** The readouts are almost
   monochrome on purpose. Accent marks only the short bracketed source
   prefix, amber only the handful of tokens that mean something is
   being lost, red only a whole error line. Colouring every verb and
   every warning turned a twenty-line preview into confetti and made
   the one line that mattered impossible to find.
"""
from __future__ import annotations

import re
import sys
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk

# --------------------------------------------------------------- palette
# The faceplate. These are the tokens that would flip in a light theme.
CHASSIS = "#31363d"      # the case
PANEL = "#3c424a"        # raised surface
PANEL_HI = "#464d56"     # hover / active surface, title bars
HAIR_D = "#22262b"       # dark hairline  (bottom/right of an outset frame)
HAIR_L = "#525a64"       # light hairline (top/left of an outset frame)
INK = "#dfe3e7"
INK_2 = "#a8b0b8"
INK_3 = "#7c858f"
ACCENT = "#6d9fd0"       # steel blue - used sparingly
# The accent only clears 3.1:1 on a lit PANEL_HI plate, which made the
# selected DAW the least legible text in the window. This tint is the
# same hue at 4.7:1 and exists solely for accent ink on a raised plate;
# on the readout, plain ACCENT is already 6.7:1 and stays.
ACCENT_HI = "#9cc4e8"
LED_GOOD = "#63c07a"
LED_CRIT = "#cf5b4e"
METER_MID = "#d9a83f"

# The readout ground. Fixed dark in every theme.
DISPLAY = "#101315"
DISPLAY_EDGE = "#0a0c0d"

# Ink used ON the readout. Fixed literals, deliberately not aliases of
# the tokens above - see the module docstring.
READOUT_INK = "#98a2a8"
READOUT_BRIGHT = "#dfe4e7"
READOUT_DIM = "#828c92"
READOUT_ACCENT = "#6d9fd0"
READOUT_GOOD = "#63c07a"
READOUT_WARN = "#d9a83f"
READOUT_CRIT = "#cf5b4e"
READOUT_SELECT = "#25333f"

PALETTE = {
    "chassis": CHASSIS, "panel": PANEL, "panel-hi": PANEL_HI,
    "hair-d": HAIR_D, "hair-l": HAIR_L,
    "ink": INK, "ink-2": INK_2, "ink-3": INK_3,
    "accent": ACCENT, "accent-hi": ACCENT_HI,
    "led-good": LED_GOOD, "led-crit": LED_CRIT,
    "meter-mid": METER_MID,
    "display": DISPLAY, "display-edge": DISPLAY_EDGE,
}

# ----------------------------------------------------------------- type
# Sizes are negative = pixels, so the scale matches the identity artifact
# exactly instead of drifting with the screen's DPI-to-point conversion.
#
# Weight and slant are always stated explicitly. "Segoe UI Semibold" is
# a real installed family on Windows that Tk resolves to an *italic*
# face and will not be talked out of, even when asked for roman - which
# quietly put the wordmark and every legend in the window into italics.
# Bold on the plain family is the reliable way to get a heavier face.
_UI = ("Segoe UI", "Segoe UI Variable Text", "Helvetica Neue", "Helvetica", "Arial")
_MONO = ("Cascadia Mono", "Consolas", "SF Mono", "DejaVu Sans Mono", "Courier New")

FONTS: dict[str, tkfont.Font] = {}


def _first_available(root: tk.Misc, candidates: tuple[str, ...]) -> str:
    """First family Tk actually resolves, so we never fall back silently.

    Not `tkfont.families()`: on stock Windows 10 that roster lists
    "Segoe UI Semibold", "Segoe UI Light" and friends but *not* plain
    "Segoe UI", so a membership test dropped the entire interface to
    Arial while the code read as if it had asked for Segoe. Making the
    font and asking what it resolved to is the honest test - Tk
    substitutes a default family for anything it doesn't have, so a
    family that answers to its own name is really installed.
    """
    for family in candidates:
        try:
            probe = tkfont.Font(root=root, family=family, size=-12)
            resolved = str(probe.actual("family"))
        except tk.TclError:  # pragma: no cover - only without a display
            return candidates[-1]
        if resolved.lower() == family.lower():
            return family
    return candidates[-1]


def _font(root: tk.Misc, family: str, size: int, bold: bool = False) -> tkfont.Font:
    return tkfont.Font(root=root, family=family, size=size,
                       weight="bold" if bold else "normal", slant="roman")


def _build_fonts(root: tk.Misc) -> dict[str, tkfont.Font]:
    ui = _first_available(root, _UI)
    mono = _first_available(root, _MONO)
    return {
        "wordmark": _font(root, ui, -19, bold=True),
        "body": _font(root, ui, -14),
        "button": _font(root, ui, -13),
        # The era's tell: small, uppercase, wide tracking, never shouting.
        "legend": _font(root, ui, -11, bold=True),
        "selector": _font(root, ui, -13, bold=True),
        "mono": _font(root, mono, -13),
        "mono_small": _font(root, mono, -12),
    }


def tracked(text: str) -> str:
    """Uppercase with letterspacing faked by thin spaces.

    Tk has no letter-spacing, and 10-11px tracked caps are the single
    most recognisable detail of the look. A U+2009 THIN SPACE between
    glyphs lands close to the artifact's .19em without the shouty gaps a
    full space would give.
    """
    return " ".join(text.upper())


# ---------------------------------------------------------------- styles
def apply(root: tk.Misc) -> dict[str, tkfont.Font]:
    """Install the palette and the ttk styles. Returns the font scale."""
    global FONTS

    style = ttk.Style(root)
    # Not optional - see the module docstring.
    style.theme_use("clam")
    FONTS = _build_fonts(root)
    root.configure(background=CHASSIS)

    style.configure(
        ".",
        background=CHASSIS,
        foreground=INK,
        fieldbackground=DISPLAY,
        troughcolor=DISPLAY_EDGE,
        bordercolor=HAIR_D,
        lightcolor=HAIR_L,
        darkcolor=HAIR_D,
        focuscolor=INK_3,
        font=FONTS["body"],
    )

    # Surfaces. Flat fills; the depth is all in the one-pixel frames.
    style.configure("TFrame", background=CHASSIS)
    style.configure("Chassis.TFrame", background=CHASSIS)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("Bar.TFrame", background=PANEL_HI)

    style.configure("TLabel", background=CHASSIS, foreground=INK)
    # Legends are INK_2, not INK_3: 11px tracked caps in INK_3 measured
    # 3.3:1 on the chassis and read as greyed-out rather than quiet.
    # INK_2 clears 5.5:1 and is still plainly subordinate to body ink.
    style.configure("Legend.TLabel", background=CHASSIS, foreground=INK_2,
                    font=FONTS["legend"])
    style.configure("Wordmark.TLabel", background=PANEL_HI, foreground=INK,
                    font=FONTS["wordmark"])
    style.configure("BarLegend.TLabel", background=PANEL_HI, foreground=INK_2,
                    font=FONTS["legend"])

    # Buttons: an outset hairline, light on top/left and dark on
    # bottom/right, that inverts when pressed. No raised plastic.
    #
    # The plate rests on PANEL and lifts to PANEL_HI under the pointer.
    # It used to rest on PANEL_HI and drop to PANEL on hover, so moving
    # the mouse over a button made it look half-pressed - backwards for
    # something meant to behave like a physical key, and it left hover
    # and pressed on the same side of the resting colour.
    style.configure(
        "TButton",
        background=PANEL,
        foreground=INK,
        font=FONTS["button"],
        borderwidth=1,
        relief="raised",
        padding=(14, 7),
        bordercolor=HAIR_D,
        lightcolor=HAIR_L,
        darkcolor=HAIR_D,
        focuscolor=INK_2,
        anchor="center",
    )
    style.map(
        "TButton",
        background=[("disabled", CHASSIS), ("pressed", CHASSIS), ("active", PANEL_HI)],
        foreground=[("disabled", INK_3)],
        lightcolor=[("pressed", HAIR_D), ("disabled", HAIR_D)],
        darkcolor=[("pressed", HAIR_L), ("disabled", HAIR_D)],
        bordercolor=[("disabled", HAIR_D)],
        relief=[("pressed", "sunken")],
    )

    # The path field is data, so it gets the recessed treatment and the
    # mono face - with the readout's fixed ink, not a chassis token.
    style.configure(
        "TEntry",
        foreground=READOUT_BRIGHT,
        fieldbackground=DISPLAY,
        insertcolor=ACCENT,
        selectbackground=READOUT_SELECT,
        selectforeground=READOUT_BRIGHT,
        bordercolor=DISPLAY_EDGE,
        lightcolor=DISPLAY_EDGE,
        darkcolor=HAIR_L,
        borderwidth=1,
        padding=(9, 7),
        font=FONTS["mono"],
    )
    style.map("TEntry", foreground=[("disabled", READOUT_DIM)],
              lightcolor=[("focus", ACCENT)])

    # A dark dot in a dark well: on screen the *unselected* indicator
    # read as the filled one and the 8px accent pip was easy to miss
    # entirely. Which DAW you are about to write into is the one thing
    # in this window that must never be ambiguous, so the DAW choice
    # uses `Selector.TRadiobutton` below instead. This style stays
    # correct for any ordinary radio that turns up later.
    style.configure(
        "TRadiobutton",
        background=CHASSIS,
        foreground=INK,
        font=FONTS["body"],
        indicatorsize=11,
        indicatorbackground=DISPLAY,
        indicatorforeground=ACCENT,
        upperbordercolor=HAIR_D,
        lowerbordercolor=HAIR_L,
        focuscolor=INK_3,
        padding=(0, 5),
    )
    style.map(
        "TRadiobutton",
        background=[("active", CHASSIS)],
        foreground=[("disabled", INK_3)],
        indicatorbackground=[("selected", DISPLAY), ("active", "#171c1f")],
        indicatorforeground=[("selected", ACCENT), ("disabled", INK_3)],
    )

    # The source-select switch: two latching segments, the live one lit.
    # Borrowing Toolbutton's layout drops the indicator and leaves a
    # plate that maps cleanly off `selected`.
    style.layout("Selector.TRadiobutton", style.layout("Toolbutton"))
    # An unlit segment is dark plastic, a lit one glows: the plate goes
    # HAIR_D -> PANEL_HI and the label INK_2 -> ACCENT_HI, so the state
    # survives even if someone is colourblind to the blue. `selected`
    # is listed before `active` so hovering the live segment does not
    # dim it back down.
    style.configure(
        "Selector.TRadiobutton",
        background=HAIR_D,
        foreground=INK_2,
        font=FONTS["selector"],
        borderwidth=0,
        relief="flat",
        padding=(18, 7),
        anchor="center",
        focuscolor=HAIR_D,
    )
    # `focus` is mapped too: Toolbutton's layout has no focus ring
    # element, so without this a keyboard user tabbing onto the switch
    # gets no indication at all that they are on it.
    style.map(
        "Selector.TRadiobutton",
        background=[("selected", PANEL_HI), ("active", PANEL),
                    ("focus", PANEL), ("disabled", HAIR_D)],
        foreground=[("selected", ACCENT_HI), ("disabled", HAIR_L),
                    ("active", INK), ("focus", INK)],
    )

    # ttk's scrollbar is drawn by clam, unlike the classic tk one - see
    # the module docstring. gripcount 0 keeps the thumb a plain slug.
    for name in ("TScrollbar", "Readout.Vertical.TScrollbar"):
        style.configure(name, background=PANEL, troughcolor=DISPLAY_EDGE,
                        bordercolor=DISPLAY_EDGE, arrowcolor=INK_3,
                        lightcolor=PANEL, darkcolor=PANEL,
                        gripcount=0, borderwidth=0, relief="flat",
                        arrowsize=11, width=11)
        # ttk marks a scrollbar disabled when everything already fits.
        # Painting the whole thumb out in that state means a readout
        # with nothing to scroll shows an empty gutter instead of a
        # full-height slug pretending to be a control.
        style.map(name,
                  background=[("disabled", DISPLAY_EDGE), ("active", PANEL_HI)],
                  lightcolor=[("disabled", DISPLAY_EDGE)],
                  darkcolor=[("disabled", DISPLAY_EDGE)],
                  arrowcolor=[("disabled", "#1b2024")])

    return FONTS


# ----------------------------------------------------------- constructions
SCROLLBAR = "Readout.Vertical.TScrollbar"


class Recess(tk.Frame):
    """A machined inset bezel: shadow top/left, highlight bottom/right.

    ttk's relief options draw a symmetric frame, so the edge is built
    from nested one-pixel frames instead. Three of them, because two
    was not enough: with only a `DISPLAY_EDGE` line against the display
    the top and left edges vanished into the readout itself and the
    thing read as a dark rectangle someone had pasted on. The outer
    hairline is `HAIR_D`, which is dark against the chassis but clearly
    lighter than the well, so the shadow side actually shows.

    Grid or pack the Recess itself; put content in `.well` (or hand it
    to `.mount()`).
    """

    def __init__(self, parent: tk.Misc):
        super().__init__(parent, background=HAIR_D)          # shadow, top/left
        highlight = tk.Frame(self, background=HAIR_L)        # highlight, b/r
        highlight.pack(fill="both", expand=True, padx=(1, 0), pady=(1, 0))
        self.well = tk.Frame(highlight, background=DISPLAY_EDGE)  # inner lip
        self.well.pack(fill="both", expand=True, padx=(0, 1), pady=(0, 1))

    def mount(self, widget: tk.Misc) -> tk.Misc:
        widget.pack(fill="both", expand=True, padx=1, pady=1)
        return widget


class Segmented(tk.Frame):
    """A hairline-boxed row of latching segments - the faceplate's
    source-select switch. Children are built with the Segmented as
    their parent and added in order; style them `Selector.TRadiobutton`.
    """

    def __init__(self, parent: tk.Misc):
        super().__init__(parent, background=HAIR_D, padx=1, pady=1)
        self._count = 0

    def add(self, widget: tk.Misc) -> tk.Misc:
        if self._count:
            tk.Frame(self, background=HAIR_D, width=1).pack(side="left", fill="y")
        widget.pack(side="left", fill="y")
        self._count += 1
        return widget


def separator(parent: tk.Misc, orient: str = "horizontal",
              background: str = CHASSIS) -> tk.Frame:
    """A machined hairline groove: one dark pixel, one light pixel."""
    holder = tk.Frame(parent, background=background)
    if orient == "horizontal":
        tk.Frame(holder, background=HAIR_D, height=1).pack(fill="x")
        tk.Frame(holder, background=HAIR_L, height=1).pack(fill="x")
    else:
        tk.Frame(holder, background=HAIR_D, width=1).pack(side="left", fill="y")
        tk.Frame(holder, background=HAIR_L, width=1).pack(side="left", fill="y")
    return holder


LED_WELL = "#242b2f"  # the ring of housing a lamp sits in, on DISPLAY


def led(parent: tk.Misc, size: int = 14, colour: str = LED_GOOD) -> tk.Canvas:
    """A single indicator lamp. Only ever mount one inside a Recess.

    Tk does not antialias canvas ovals, and under about ten pixels a
    circle comes out as a lopsided plus sign - the original 8px lamp
    read as a smudge. The lit disc is `size - 4`, so keep `size` at 13
    or more or the cross comes back.
    """
    canvas = tk.Canvas(parent, width=size, height=size, highlightthickness=0,
                       background=DISPLAY, borderwidth=0)
    canvas.create_oval(0, 0, size - 1, size - 1, fill=LED_WELL, outline="")
    canvas.create_oval(2, 2, size - 3, size - 3, fill=colour, outline="",
                       tags="lamp")
    return canvas


def set_led(canvas: tk.Canvas, colour: str) -> None:
    canvas.itemconfigure("lamp", fill=colour)


# ------------------------------------------------------------------- mark
# Two heads facing across a lit span: the two colours are the two DAWs and
# the bright column between them is the shared session.
#
# The geometry is declared once, as flat shapes on a 64-unit square, and
# three renderers walk the same list - the Tk canvas in the header, the
# .ico the build stamps on the exe, and the SVG on the identity page - so
# the badge in the app and the badge in the spec cannot drift apart.
# Anything all three cannot draw identically (gradients, real curves,
# rounded corners) is deliberately absent, and the shapes are fills rather
# than strokes: Tk does not antialias its canvas at all, and a monoline
# mark needs a different stroke weight at every size, which one drawing
# cannot give it.
#
# The mark carries its own dark ground, which is the readout rule again -
# a badge is dark whatever colour the chassis is. That is not only taste.
# Measured on the light "brushed aluminium" chassis, every candidate
# accent dies: steel blue 1.3:1, the orange 1.5:1, the red 2.3:1. On the
# tile the same colours clear 3.9-6.8:1 and the theme cannot touch them.
# A taskbar icon has the same problem with more unknowns, since it brings
# its own background.
MARK_TILE = "#0e1114"
MARK_METAL = "#e2e7ea"

# The mark's warm colour. A *brand* colour, not a state colour: it belongs
# to the badge and the icon and goes nowhere near the panel, because
# green, amber and red each have to keep meaning exactly one thing.
SIGNAL_WAYS = {
    "red": "#d2382a",      # the 1073 red
    "orange": "#f26a1f",   # signal orange - console illumination, tape gear
    "copper": "#c98a46",   # metal rather than signal - the quiet one
}

# The two lines that change if the colourway is re-cut.
MARK = "interlock"
COLOURWAY = "orange"

# ("rect", x, y, w, h, role) | ("poly", [(x, y), ...], role)
#
# Nothing here is narrower than eight units - two pixels at icon size -
# and the two heads are twenty. That is the constraint that decided the
# proportions: drawn thinner, the seam closed up at 16px and the mark
# went to a smudge.
MARKS: dict[str, list[tuple]] = {
    "interlock": [
        ("rect", 0, 0, 64, 64, "tile"),
        ("poly", [(8, 10), (27, 28), (27, 36), (8, 54)], "cool"),
        ("poly", [(56, 10), (37, 28), (37, 36), (56, 54)], "hot"),
        ("rect", 28, 4, 8, 56, "metal"),
    ],
}


def mark_palette(colourway: str = COLOURWAY) -> dict[str, str]:
    return {
        "tile": MARK_TILE,
        "hot": SIGNAL_WAYS[colourway],
        "cool": ACCENT,
        "metal": MARK_METAL,
    }


def mark_shapes(name: str = MARK, colourway: str = COLOURWAY,
                size: int = 64) -> list[tuple]:
    """The mark resolved to pixel coordinates and literal colours.

    Returns ("rect", x0, y0, x1, y1, colour) and ("poly", points, colour)
    with the corners already scaled. Both renderers below and the .ico
    builder in `tools/build_exe.py` consume this and nothing else.
    """
    palette = mark_palette(colourway)
    k = size / 64
    out: list[tuple] = []
    for shape in MARKS[name]:
        if shape[0] == "rect":
            _, x, y, w, h, role = shape
            out.append(("rect", x * k, y * k, (x + w) * k, (y + h) * k,
                        palette[role]))
        else:
            _, points, role = shape
            out.append(("poly", [(px * k, py * k) for px, py in points],
                        palette[role]))
    return out


def mark_canvas(parent: tk.Misc, size: int = 26, name: str = MARK,
                colourway: str = COLOURWAY) -> tk.Canvas:
    """The mark as a Tk widget, for the header bar."""
    canvas = tk.Canvas(parent, width=size, height=size, highlightthickness=0,
                       borderwidth=0, background=MARK_TILE)
    for shape in mark_shapes(name, colourway, size):
        if shape[0] == "rect":
            _, x0, y0, x1, y1, colour = shape
            canvas.create_rectangle(x0, y0, x1, y1, fill=colour, outline="")
        else:
            _, points, colour = shape
            canvas.create_polygon(points, fill=colour, outline="")
    return canvas


def mark_svg(name: str = MARK, colourway: str = COLOURWAY,
             size: int | None = None) -> str:
    """The same mark as standalone SVG, for `assets/` and the artifact."""
    attrs = f' width="{size}" height="{size}"' if size else ""
    body = []
    for shape in mark_shapes(name, colourway, 64):
        if shape[0] == "rect":
            _, x0, y0, x1, y1, colour = shape
            body.append(f'  <rect x="{x0:g}" y="{y0:g}" width="{x1 - x0:g}"'
                        f' height="{y1 - y0:g}" fill="{colour}"/>')
        else:
            _, points, colour = shape
            pts = " ".join(f"{x:g},{y:g}" for x, y in points)
            body.append(f'  <polygon points="{pts}" fill="{colour}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"'
            f'{attrs} role="img" aria-label="DAWBridge">\n'
            + "\n".join(body) + "\n</svg>\n")


def asset_path(name: str) -> Path | None:
    """Find a bundled asset both frozen and from source, or None.

    PyInstaller unpacks --add-data into a temp directory it points
    `sys._MEIPASS` at; from source the same file sits in the repo. Get
    this wrong in one direction and it works in dev and breaks in the
    build, which is the whole reason it is a function.
    """
    root = getattr(sys, "_MEIPASS", None)
    base = Path(root) if root else Path(__file__).resolve().parent.parent
    found = base / "assets" / name
    return found if found.exists() else None


def apply_window_icon(root: tk.Misc, name: str = "dawbridge.ico") -> bool:
    """Wear the mark in the title bar and the taskbar. Never fatal.

    The .ico is generated by `tools/build_exe.py`; if it has not been
    built yet the app runs with Tk's default icon rather than refusing
    to start over an icon.
    """
    path = asset_path(name)
    if path is None:
        return False
    try:
        root.iconbitmap(default=str(path))
    except tk.TclError:
        return False
    return True


# --------------------------------------------------------------- readouts
def configure_readout(text: tk.Text, small: bool = False) -> tkfont.Font:
    """Dress a Text/ScrolledText as a recessed instrument readout.

    Returns the face it was given: `row_pitch` needs it to work out how
    tall one row of this readout is.
    """
    face = FONTS["mono_small" if small else "mono"]
    text.configure(
        background=DISPLAY,
        foreground=READOUT_INK,
        insertbackground=ACCENT,
        selectbackground=READOUT_SELECT,
        selectforeground=READOUT_BRIGHT,
        inactiveselectbackground=READOUT_SELECT,
        font=face,
        relief="flat",
        borderwidth=0,
        highlightthickness=0,
        padx=12,
        pady=9,
        # Leading, split above and below each paragraph. `spacing2` is
        # deliberately spacing1 + spacing3: it is the gap *inside* a
        # wrapped line, and matching the sum makes every display row the
        # same pitch whether or not it is a continuation. Leave it at 0
        # and a wrapped line is three pixels short of a whole row, which
        # is enough to put `snap_rows` back off the grid.
        spacing1=2,
        spacing2=3,
        spacing3=1,
        wrap="word",
        cursor="arrow",
    )
    # Order matters: a Text tag created later outranks an earlier one
    # where they overlap. The detail block paints `dim` across a whole
    # line and then re-marks the verb, so `dim` has to be the weakest of
    # the set or the verb column silently stays dim.
    for name, colour in (
        ("dim", READOUT_DIM),
        ("bright", READOUT_BRIGHT),
        ("accent", READOUT_ACCENT),
        ("good", READOUT_GOOD),
        ("warn", READOUT_WARN),
        ("crit", READOUT_CRIT),
    ):
        text.tag_configure(name, foreground=colour)
    return face


def row_pitch(text: tk.Text, face: tkfont.Font) -> int:
    """Pixel height of one row of a readout, leading included.

    Not `face.metrics("linespace")` on its own, and not the Text's own
    idea of it either: Tk sizes a Text of `height` rows as height *
    linespace and ignores the spacing options entirely, so a readout
    asked for eight rows is 24 pixels short of showing eight.
    """
    return (face.metrics("linespace") + int(text.cget("spacing1"))
            + int(text.cget("spacing3")))


def snap_rows(pane: tk.Misc, text: tk.Text, face: tkfont.Font,
              slack_row: int = 1) -> None:
    """Keep a readout's visible area a whole number of rows.

    A Text stretched to a pixel height fits as many rows as it can and
    then draws part of one more against the bezel. A real display never
    shows half a character row, so the sliced row reads as a broken
    widget rather than as a detail - and in the log, which scrolls to
    the bottom, the slice lands at the *top* where it is the first thing
    the eye hits.

    The pane's height is dictated by the window, so the rounding has to
    go somewhere else: `text` occupies row 0 of `pane` and takes the
    whole-row part, and the leftover - never as much as one row - is
    parked in `slack_row` below it, where it reads as the readout's
    bottom padding. That row stays empty on purpose. A spacer *widget*
    there was the obvious build and it has a hole in it: a Frame asked
    for a height of zero keeps whatever height it last had, so every
    window size that happened to need no slack at all stayed stuck on
    the previous size's offset. An empty row with a `minsize` takes 0
    for the answer it is.

    `pane` must have grid propagation switched off. With it on, the
    slack feeds back into the pane's requested size, the toplevel grows
    to match, the pane gets taller, and the window walks itself up the
    screen a row at a time.
    """
    pitch = row_pitch(text, face)
    chrome = 2 * int(text.cget("pady"))
    applied = [-1]

    def on_configure(event: tk.Event) -> None:
        usable = event.height - chrome
        rows = max(1, usable // pitch)
        slack = max(0, usable - rows * pitch)
        if slack != applied[0]:
            applied[0] = slack
            pane.rowconfigure(slack_row, minsize=slack)

    pane.bind("<Configure>", on_configure)


def natural_height(rows: int, text: tk.Text, face: tkfont.Font) -> int:
    """Pixels a readout needs to show `rows` rows and its padding."""
    return rows * row_pitch(text, face) + 2 * int(text.cget("pady"))


def placeholder(text: tk.Text, message: str) -> None:
    """Dim standby copy so an empty readout looks idle, not broken."""
    was = str(text.cget("state"))
    text.configure(state="normal")
    text.delete("1.0", "end")
    text.insert("1.0", message, ("dim",))
    text.configure(state=was)


# ------------------------------------------------------------ log tagging
# One preview dumps twenty-odd lines, so the palette has to survive
# density. An earlier version painted every warning line amber end to
# end and gave each change verb its own colour; a normal preview came
# out as four hues of confetti with three solid amber lines shouting
# over the one red one that actually mattered.
#
# What is left: a whole line is only ever coloured for [error]. Warnings
# get an amber prefix and an ordinary body. The indented detail block is
# dim, with the verb column a step brighter so it still reads as a
# column - and amber on exactly the two verbs that mean something is
# being dropped rather than changed.
_PREFIX = re.compile(r"^(?:\[[a-z]+\])+")
_VERB = re.compile(r"^\s+([A-Z]{3,7})\b")
_LOSSY_VERBS = {"MUTE", "ORPHAN"}


def tag_log_line(text: tk.Text, start: str, line: str) -> None:
    """Colour one just-inserted log line. `start` is its first index."""
    end = f"{start} lineend"
    if "[error]" in line:
        text.tag_add("crit", start, end)
        return
    if line[:1].isspace():
        text.tag_add("dim", start, end)
        match = _VERB.match(line)
        if match:
            tag = "warn" if match.group(1) in _LOSSY_VERBS else "bright"
            text.tag_add(tag, f"{start}+{match.start(1)}c",
                         f"{start}+{match.end(1)}c")
        return
    match = _PREFIX.match(line)
    if match:
        tag = "warn" if "[warning]" in match.group() else "accent"
        text.tag_add(tag, start, f"{start}+{match.end()}c")


_BRACKET_ID = re.compile(r"\[[^\]]+\]")


def tag_status(text: tk.Text) -> None:
    """Colour the whole session-status block after it has been written.

    The bracketed ids used to be accent, which put the brightest colour
    in the pane on eight lines of hex nobody reads and left the track
    names - the only part an end user cares about - unemphasised. They
    are dim now, so the eye lands on the names.
    """
    last = int(str(text.index("end-1c")).split(".")[0])
    for row in range(1, last + 1):
        line = text.get(f"{row}.0", f"{row}.end")
        if not line.strip():
            continue
        if row == 1:
            text.tag_add("bright", f"{row}.0", f"{row}.end")
        elif line.startswith("last updated") or line.endswith(":"):
            text.tag_add("dim", f"{row}.0", f"{row}.end")
        match = _BRACKET_ID.search(line)
        if match:
            text.tag_add("dim", f"{row}.{match.start()}", f"{row}.{match.end()}")

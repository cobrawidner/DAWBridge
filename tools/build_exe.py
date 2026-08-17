"""Build the standalone DAWBridge.exe, icon and all.

    python tools/build_exe.py            # icon, then exe
    python tools/build_exe.py --icon-only

A script rather than a remembered command line so the build is one step
and always the same.

The icon is drawn from `dawbridge.theme.MARKS` rather than traced from a
file, so the mark in the header, the icon on the exe and the SVG in the
identity artifact are all the same drawing and cannot drift apart.
Re-cutting the colourway is one line in `theme.py`.

Pillow is a *build* dependency only. Nothing under `dawbridge/` imports
it, so the shipped exe does not carry it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dawbridge import theme  # noqa: E402  (needs ROOT on the path first)

ICON = ROOT / "assets" / "dawbridge.ico"

# Built separately by tools/build_reaper_runtime.py, and not in git - it
# is ~10MB of third-party binaries. Name kept in step with
# dawbridge.reapersetup.RUNTIME_ASSET, which is what looks for it at run
# time.
REAPER_RUNTIME = "reaper_runtime.zip"
# What Windows actually asks for. 16 is the taskbar and the title bar,
# and it is the only one that decides whether a mark works.
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
SUPERSAMPLE = 8


def render(size: int, name: str = theme.MARK,
           colourway: str = theme.COLOURWAY):
    """One frame of the icon, drawn big and resampled down.

    Tk has no antialiasing, so the header badge is drawn at final size
    and lives with it. An icon is scaled by the shell and every jagged
    diagonal shows, so each frame is drawn at 8x and resampled - which
    is what gives the two heads clean edges at 16 pixels.
    """
    from PIL import Image, ImageDraw

    big = size * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for shape in theme.mark_shapes(name, colourway, big):
        if shape[0] == "rect":
            _, x0, y0, x1, y1, colour = shape
            draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=colour)
        else:
            _, points, colour = shape
            draw.polygon(points, fill=colour)
    return image.resize((size, size), Image.LANCZOS)


def build_icon(name: str = theme.MARK, colourway: str = theme.COLOURWAY,
               path: Path = ICON) -> Path:
    frames = [render(size, name, colourway) for size in ICON_SIZES]
    path.parent.mkdir(parents=True, exist_ok=True)
    # Every frame is drawn at its own size rather than letting the ICO
    # writer resample one big frame down, so the 16px entry gets the
    # supersampling above instead of a generic thumbnail.
    frames[-1].save(path, format="ICO",
                    sizes=[(s, s) for s in ICON_SIZES],
                    append_images=frames[:-1])
    print(f"icon  {path}  {', '.join(str(s) for s in ICON_SIZES)}")
    return path


def write_svg(path: Path = ROOT / "assets" / "dawbridge_mark.svg") -> Path:
    path.write_text(theme.mark_svg(), encoding="utf-8")
    print(f"svg   {path}")
    return path


def main() -> int:
    build_icon()
    write_svg()
    if "--icon-only" in sys.argv:
        return 0

    separator = ";" if sys.platform == "win32" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--name", "DAWBridge",
        # --icon stamps the exe; --add-data ships the same file so the
        # window itself can wear it too. Anything added here has to be
        # resolved through sys._MEIPASS at runtime - see theme.asset_path.
        "--icon", str(ICON),
        "--add-data", f"{ICON}{separator}assets",
        str(ROOT / "run_gui.py"),
    ]

    # The interpreter Reaper loads. Without it the .exe still runs and
    # Pro Tools still works, but a Reaper collaborator is back to
    # installing Python by hand - which is the thing this removes. Warn
    # loudly rather than shipping a build that quietly lost the feature.
    runtime = ROOT / "assets" / REAPER_RUNTIME
    if runtime.exists():
        cmd.insert(-1, "--add-data")
        cmd.insert(-1, f"{runtime}{separator}assets")
    else:
        print(f"WARNING: {runtime} is missing, so this build cannot set Reaper up on its own.")
        print("         Build it first: python tools/build_reaper_runtime.py")
    print(" ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode == 0:
        print(f"\nbuilt {ROOT / 'dist' / 'DAWBridge.exe'}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

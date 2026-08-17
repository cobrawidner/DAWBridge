"""Build the Python interpreter DAWBridge ships for Reaper to load.

Reaper runs reapy's server ReaScript on an interpreter it loads itself,
named in `reaper.ini`. Before this existed, that had to be a Python the
collaborator installed by hand - the one setup step nobody could get
through unaided. See dawbridge/reapersetup.py for the full reasoning.

This produces `assets/reaper_runtime.zip`: python.org's Windows
*embeddable* distribution (published for precisely this - an interpreter
meant to be shipped inside another application) with reapy installed
into it. `tools/build_exe.py` bundles the result as a data file.

Run it before building the .exe:

    python tools/build_reaper_runtime.py

It is skipped when the zip already exists, so the usual build doesn't
re-download 10MB. Pass --force to rebuild.

Deliberately NOT committed to the repo: it is ~15MB of third-party
binaries that git would carry forever, and CI can rebuild it in seconds
from a URL that is stable and versioned.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "assets" / "reaper_runtime.zip"

# Pinned, not "latest". Reaper loads this DLL into its own process, so a
# surprise version bump is a crash in someone's DAW rather than a failed
# build. 3.10 is what the Reaper backend was developed and live-tested
# against; move it deliberately, and re-run the live checks when you do.
PYTHON_VERSION = "3.10.11"
EMBED_URL = (f"https://www.python.org/ftp/python/{PYTHON_VERSION}/"
             f"python-{PYTHON_VERSION}-embed-amd64.zip")

# 64-bit only, matching the .exe and every modern Reaper. A 32-bit Reaper
# would need a separate runtime and its own `pythonlibdll32` keys; if that
# ever comes up it is a new bundle, not a tweak to this one.


def download(url: str, dest: Path) -> None:
    print(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as response:
        dest.write_bytes(response.read())


def enable_site_packages(staging: Path) -> None:
    """Let the embedded interpreter import installed packages.

    The embeddable distribution ships a `._pth` file that pins sys.path
    to the stdlib zip and switches off site processing - it is meant to
    run one known application, not to be a general Python. reapy has to
    be importable from it, so `Lib\\site-packages` is added and `import
    site` uncommented. Without this the interpreter loads, Reaper reports
    no error, and `import reapy` fails inside a ReaScript where nobody
    sees the traceback.
    """
    pth_files = list(staging.glob("python3*._pth"))
    if not pth_files:
        raise RuntimeError(f"no ._pth file in {staging} - unexpected embeddable layout")
    pth = pth_files[0]
    lines = pth.read_text(encoding="utf-8").splitlines()

    site_dir = r"Lib\site-packages"
    if site_dir not in lines:
        lines.append(site_dir)
    lines = ["import site" if line.strip() == "#import site" else line for line in lines]
    if "import site" not in [line.strip() for line in lines]:
        lines.append("import site")

    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"enabled site-packages in {pth.name}")


def install_reapy(staging: Path) -> None:
    """Install reapy into the staged runtime.

    `--target` with the *building* interpreter's pip, rather than trying
    to bootstrap pip inside the embeddable distribution (which has no
    ensurepip). reapy is pure Python, so nothing here is tied to the
    interpreter that did the installing.
    """
    target = staging / "Lib" / "site-packages"
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade",
         "--target", str(target), "python-reapy"],
        check=True,
    )
    server = target / "reapy" / "reascripts" / "activate_reapy_server.py"
    if not server.exists():
        raise RuntimeError(
            f"reapy installed but {server.relative_to(staging)} is missing - Reaper would "
            f"be pointed at a script that isn't there"
        )
    print(f"installed reapy, server script at {server.relative_to(staging)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true",
                        help="rebuild even if the runtime zip already exists")
    args = parser.parse_args()

    if OUTPUT.exists() and not args.force:
        print(f"{OUTPUT} already built - pass --force to rebuild")
        return 0

    staging = ROOT / "build" / "reaper-runtime"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    embed_zip = staging.parent / f"python-{PYTHON_VERSION}-embed-amd64.zip"
    if not embed_zip.exists():
        download(EMBED_URL, embed_zip)
    with zipfile.ZipFile(embed_zip) as bundle:
        bundle.extractall(staging)

    if not list(staging.glob("python3*.dll")):
        raise RuntimeError(f"no python3XX.dll in {embed_zip} - wrong archive?")

    enable_site_packages(staging)
    install_reapy(staging)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # ZIP_DEFLATED, not stored: this rides inside the .exe and every
    # collaborator downloads it.
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as out:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                out.write(path, path.relative_to(staging))

    print(f"\nbuilt {OUTPUT} ({OUTPUT.stat().st_size / 1_000_000:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

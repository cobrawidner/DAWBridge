"""Make Reaper reachable without anyone installing Python.

**The problem this exists to remove.** reapy is a client/server pair.
The client half ships inside DAWBridge.exe, but the server half is a
ReaScript that runs *inside Reaper*, on an interpreter Reaper loads
itself. So `reaper.ini` has to name a Python DLL:

    reascript       = 1
    pythonlibpath64 = C:\\...\\Python310
    pythonlibdll64  = python310.dll

which meant every Reaper collaborator had to find, download and install
a Python of a version Reaper would accept, before DAWBridge did anything
at all. A collaborator confirmed it works and that getting there was
miserable - which is a design flaw, not a support problem. Bundling
reapy into the .exe never helped: PyInstaller's copy lives in a temp
directory that exists only while the app is open, and Reaper needs a
real DLL and a real standard library on disk.

**What this does instead.** DAWBridge carries python.org's Windows
*embeddable* distribution - the one published for exactly this, an
interpreter meant to be shipped inside another application - with reapy
already installed into it. On setup it unpacks that beside the app's own
data and points Reaper at it. Nothing is installed, nothing lands on
PATH, no existing Python on the machine is touched or consulted, and
removing the folder undoes it.

Three things here are non-negotiable, each learned from how this fails:

1. **Reaper must be closed.** Reaper rewrites `reaper.ini` from memory
   when it quits, so edits made while it is running are silently
   reverted on exit - the setup would report success and the user would
   restart into exactly the state they started in.
2. **Verify by reading back.** Every step re-reads what it wrote. The
   alternative is telling someone they're set up and letting them
   discover otherwise in front of a collaborator.
3. **Back up first.** `reaper.ini` and `reaper-kb.ini` hold preferences
   and key bindings someone may have spent years on. reapy's own helpers
   keep a `.bak` and a one-time `.before-reapy.bak`; the runtime keys
   this module writes itself go through the same Config class for that
   reason.
"""
from __future__ import annotations

import configparser
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

#: The bundled runtime, built by tools/build_reaper_runtime.py and shipped
#: as a PyInstaller data file. See theme.asset_path for how it resolves
#: both frozen and from source.
RUNTIME_ASSET = "reaper_runtime.zip"

#: Reaper's web interface port, which is also reapy's transport. Matches
#: reapy.config.WEB_INTERFACE_PORT; duplicated rather than imported so
#: this module can report status without importing reapy at all.
WEB_INTERFACE_PORT = 2307


def runtime_root() -> Path:
    """Where the unpacked interpreter lives.

    Under LOCALAPPDATA rather than beside the .exe: someone runs
    DAWBridge from Downloads, and an interpreter that vanishes when they
    tidy up would take Reaper's config with it - `reaper.ini` would still
    name a DLL that no longer exists.
    """
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "DAWBridge" / "reaper-python"


def resource_dir() -> Path | None:
    """Reaper's configuration directory, or None if it isn't found.

    Only the standard per-user location. reapy also detects portable
    installs by scanning the process table with psutil; that path needs
    exactly one running Reaper, and this whole flow requires Reaper to be
    *closed*, so the two can't both be true. A portable install gets the
    manual instructions instead of a wrong guess.
    """
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    candidate = Path(appdata) / "REAPER"
    return candidate if (candidate / "reaper.ini").exists() else None


def reaper_is_running() -> bool | None:
    """Whether Reaper is open. None when it can't be determined.

    None is a real answer and callers must treat it as one: "I couldn't
    check" has to read differently from "it's closed", because acting on
    the wrong one silently discards the whole setup (see rule 1 above).
    """
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq reaper.exe", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return "reaper.exe" in out.stdout.lower()


def find_dll(runtime: Path) -> Path | None:
    """The python3XX.dll inside an unpacked runtime, or None.

    Found by pattern rather than hardcoded, so bumping the bundled
    version in tools/build_reaper_runtime.py needs no change here.
    `python3.dll` is excluded deliberately: it is the stable-ABI
    forwarder, and Reaper needs the real versioned one.
    """
    for path in sorted(Path(runtime).glob("python3*.dll")):
        if path.stem.lower() != "python3":
            return path
    return None


def server_script(runtime: Path) -> Path:
    """reapy's server ReaScript, as unpacked on disk.

    Reaper stores this path in reaper-kb.ini and runs it at startup, so
    it has to be somewhere permanent. The copy PyInstaller unpacks into
    sys._MEIPASS is deleted when DAWBridge closes, which would leave
    Reaper pointed at a path that exists only while the app is open.
    """
    return Path(runtime) / "Lib" / "site-packages" / "reapy" / "reascripts" / \
        "activate_reapy_server.py"


def is_unpacked(runtime: Path) -> bool:
    """Whether `runtime` holds a usable interpreter.

    Checks the two files that actually get used rather than just the
    directory: a half-finished extraction (a full disk, a killed
    process) leaves a folder that looks present and works for nothing.
    """
    return find_dll(runtime) is not None and server_script(runtime).exists()


def unpack_runtime(archive: Path, runtime: Path) -> Path:
    """Extract the bundled interpreter. Returns the path to its DLL.

    Extracts to a sibling directory and moves it into place, so an
    interrupted unpack never leaves a half-populated runtime that
    `is_unpacked` would then approve.
    """
    runtime = Path(runtime)
    if is_unpacked(runtime):
        return find_dll(runtime)

    staging = runtime.with_name(runtime.name + ".unpacking")
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(staging)

    if find_dll(staging) is None:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(
            f"the bundled Reaper runtime in {archive.name} has no python3XX.dll in it - "
            f"this is a packaging fault, not something you can fix; please report it"
        )

    if runtime.exists():
        shutil.rmtree(runtime, ignore_errors=True)
    runtime.parent.mkdir(parents=True, exist_ok=True)
    staging.replace(runtime)
    return find_dll(runtime)


#: A script entry in reaper-kb.ini. reapy writes these with `open(..., "a")`
#: and no trailing newline (config.py's add_reascript), so appending a
#: second one fuses it onto the first: "...activate_reapy_server.pySCR 4 0
#: RSLv...". Reaper cannot parse the fused entry, so the action simply does
#: not exist - DAWBridge then asks Reaper to run an id Reaper has never
#: heard of, nothing happens, no Python is loaded, and reapy waits forever
#: for a server that will never start. Confirmed on a real install: a
#: 400-byte reaper-kb.ini containing two entries and zero newlines.
_FUSED_ENTRY = re.compile(r"(?<=\.py)(?=SCR 4 0 )")


def repair_script_list(resource: Path) -> bool:
    """Undo the fused-entry corruption in reaper-kb.ini. True if it changed.

    Runs before adding our own entry, so it both repairs damage already
    done and stops the next append from causing more. Deliberately narrow:
    it splits only at the exact `.py` + `SCR 4 0 ` junction that this bug
    produces, and guarantees a trailing newline. It never reorders,
    rewrites or removes an entry - this file holds key bindings someone
    may have spent years on, and a repair that "tidied" it would be worse
    than the bug.
    """
    path = Path(resource) / "reaper-kb.ini"
    if not path.exists():
        return False
    original = path.read_text(encoding="utf-8", errors="replace")

    repaired = _FUSED_ENTRY.sub(chr(10), original)
    if repaired and not repaired.endswith(chr(10)):
        repaired += chr(10)
    if repaired == original:
        return False

    # Same backup discipline as reaper.ini: keep what we are replacing.
    backup = path.with_suffix(path.suffix + ".bak")
    if not backup.exists():
        backup.write_text(original, encoding="utf-8")
    path.write_text(repaired, encoding="utf-8")
    return True


def existing_python(resource: Path) -> tuple[str, str] | None:
    """The Python Reaper is already configured to use, if it has one.

    Returns (directory, dll name), or None when nothing usable is set.

    **This is the guard that should have existed from the start.** The
    bundled interpreter exists for people who have no Python, which was
    the whole complaint. It was never meant for a machine that already
    works - and on the first one it met, it overwrote a working
    `pythonlibpath64` with its own, and broke a setup that had been fine
    for months. Shipping a convenience that damages the people who
    didn't need it is worse than not shipping it.

    "Usable" means the DLL is really on disk. A path left behind by a
    Python that has since been uninstalled is not something to preserve.
    """
    # configparser rather than reapy's Config, deliberately. This is a
    # read, so it needs none of reapy's backup-and-write care - and
    # importing reapy is itself hazardous while Reaper is up without its
    # bridge (see reaper_backend.bridge_server_state). A guard that
    # protects someone's working setup must not depend on the very thing
    # that can hang. It also has to run before any reapy import, which
    # settles the question.
    parser = configparser.ConfigParser(strict=False, delimiters="=", interpolation=None)
    parser.optionxform = str.lower
    try:
        parser.read(Path(resource) / "reaper.ini", encoding="utf-8")
        section = parser["reaper"]
        directory = str(section.get("pythonlibpath64", "")).strip()
        dll = str(section.get("pythonlibdll64", "")).strip()
    except Exception:
        return None
    if not directory or not dll:
        return None
    return (directory, dll) if (Path(directory) / dll).exists() else None


def is_bundled(directory: str) -> bool:
    """Whether a configured Python is one DAWBridge unpacked itself."""
    try:
        return Path(directory).resolve() == runtime_root().resolve()
    except Exception:
        return False


def describe_state(resource: Path | None, runtime: Path) -> list[str]:
    """Plain-language lines about what is and isn't set up yet."""
    lines = []
    if resource is None:
        lines.append("Reaper's settings folder wasn't found - is Reaper installed for this user?")
    else:
        lines.append(f"Reaper settings: {resource}")
    # Which Python Reaper will actually use matters more than whether
    # ours is unpacked, and it is the difference between "setup will do
    # something" and "setup will correctly do nothing".
    if resource is None:
        # Nothing true can be said about a Reaper that wasn't found.
        lines.append("Bundled Python: unpacked" if is_unpacked(runtime)
                     else "Bundled Python: not unpacked yet")
        return lines

    already = existing_python(resource)
    if already and not is_bundled(already[0]):
        lines.append(f"Python: Reaper already uses its own, at {already[0]}")
        lines.append("Nothing to set up - DAWBridge will not replace a working Python.")
    elif already:
        lines.append("Python: Reaper is using the copy DAWBridge unpacked.")
    else:
        lines.append("Python: Reaper has none configured - DAWBridge will supply one.")
    return lines


class PythonAlreadyWorking(RuntimeError):
    """Reaper already has a Python. Refuse rather than replace it."""

    def __init__(self, directory: str, dll: str):
        self.directory, self.dll = directory, dll
        super().__init__(
            f"Reaper is already set up to use the Python at {directory}, so DAWBridge "
            f"left it alone. Replacing a working Python is how a machine that was fine "
            f"stops being fine. If Reaper genuinely isn't connecting, fix that setup, or "
            f"clear the Python path in Reaper (Options > Preferences > Plug-ins > "
            f"ReaScript) and set up again - DAWBridge will then use its own copy."
        )


def configure(resource: Path, dll: Path, script: Path,
              replace_existing_python: bool = False) -> list[str]:
    """Point Reaper at the bundled interpreter and reapy's server script.

    Returns the steps taken, for the log. Raises with a sentence a
    musician can act on if anything can't be done.

    The ini edits are reapy's own helpers wherever one exists - they
    generate the unique command id for reaper-kb.ini and keep the
    backups, and reimplementing that would risk someone's key bindings
    to save an import. The one thing not delegated is the Python DLL
    itself: reapy's `enable_python` finds the *running* interpreter's
    library, which inside a frozen .exe is a temp path that stops
    existing the moment DAWBridge closes. That is the exact bug this
    module was written to remove, so the keys are written here instead.
    """
    from reapy.config.config import Config, add_reascript, add_web_interface, set_ext_state

    done = []
    ini = Path(resource) / "reaper.ini"

    # Never take a working Python away from someone who already had one -
    # see existing_python for what happened the one time this wasn't
    # checked. The bundled copy is for machines with nothing, which was
    # the entire point of building it.
    already = existing_python(resource)
    if already and not is_bundled(already[0]) and not replace_existing_python:
        raise PythonAlreadyWorking(*already)

    config = Config(str(ini))
    config["reaper"]["reascript"] = "1"
    config["reaper"]["pythonlibpath64"] = str(dll.parent)
    config["reaper"]["pythonlibdll64"] = dll.name
    config.write()
    done.append(f"pointed Reaper at the bundled Python ({dll.name})")

    add_web_interface(str(resource), WEB_INTERFACE_PORT)
    done.append(f"enabled Reaper's web interface on port {WEB_INTERFACE_PORT}")

    # Before adding ours: reapy's own appends leave the file with no
    # trailing newline, so a second entry fuses onto the first and Reaper
    # can parse neither. See repair_script_list.
    if repair_script_list(resource):
        done.append("repaired Reaper's action list (entries had run together)")

    # The same four steps configure_reaper() takes, minus enable_python.
    # The ext-state write is not optional bookkeeping: it is how the
    # script announces its own action name, and reapy's client looks it
    # up there to trigger the server. Without it the script is installed
    # and never runs.
    action = add_reascript(str(resource), str(script))
    set_ext_state("reapy", "activate_reapy_server", action, str(resource))
    done.append("installed the DAWBridge bridge script into Reaper's actions")

    problem = verify(resource, dll)
    if problem:
        raise RuntimeError(problem)
    return done


def verify(resource: Path, dll: Path) -> str | None:
    """Re-read reaper.ini and say what didn't take, or None if all did.

    Rule 2: a setup that reports success it didn't achieve is worse than
    one that fails, because the user finds out later and somewhere less
    convenient.
    """
    from reapy.config.config import Config

    config = Config(str(Path(resource) / "reaper.ini"))
    section = config["reaper"]
    if section.get("reascript") != "1":
        return "Reaper's ReaScript support didn't stay enabled - is Reaper still open?"
    if section.get("pythonlibdll64") != dll.name:
        return (f"Reaper's Python setting didn't stick (it says "
                f"{section.get('pythonlibdll64')!r}) - is Reaper still open? It rewrites "
                f"its settings when it closes, which undoes this.")
    return None

"""Reaper gets set up without anyone installing Python.

A collaborator got DAWBridge working and reported that installing the
right Python to get there was the miserable part. This is the code that
removes that step, so what's tested here is mostly the ways it can fail
*quietly*: a half-unpacked runtime that looks present, a config write
Reaper silently reverts, a script path that stops existing when the app
closes.

The one thing not covered is the thing no test can cover: whether Reaper
itself loads this interpreter. That needs a live Reaper.
"""
import sys
import zipfile
from pathlib import Path

import pytest

from dawbridge import reapersetup
from dawbridge.reaper_backend import NO_SERVER, bridge_server_state


def _reapy_import_would_hang() -> bool:
    """Whether `import reapy` is currently a trap on this machine.

    reapy connects at module-import time and, when Reaper is running
    without its bridge server, recurses forever trying to start one -
    see reaper_backend.bridge_server_state. A developer with Reaper open
    in that state would otherwise watch the whole suite hang with no
    output, which is a far worse experience than a named skip.

    Already-imported is always safe: the connect happened once and
    whatever it did, it finished.
    """
    if "reapy" in sys.modules:
        return False
    return bridge_server_state() == NO_SERVER


#: The tests below drive reapy's real ini writers rather than fakes,
#: because the whole point of them is that reapy is called with OUR
#: interpreter and OUR script path. That means really importing it.
needs_reapy = pytest.mark.skipif(
    _reapy_import_would_hang(),
    reason="Reaper is running without its bridge server; `import reapy` would never return",
)


def _fake_runtime_zip(tmp_path: Path, *, dll: str = "python310.dll",
                      with_server: bool = True) -> Path:
    """A stand-in for the real 10MB bundle, same layout."""
    archive = tmp_path / "reaper_runtime.zip"
    with zipfile.ZipFile(archive, "w") as out:
        if dll:
            out.writestr(dll, "not really a dll")
        out.writestr("python310._pth", "python310.zip\n.\nLib\\site-packages\nimport site\n")
        if with_server:
            out.writestr("Lib/site-packages/reapy/reascripts/activate_reapy_server.py",
                         "# server")
    return archive


# ---- finding the pieces ------------------------------------------------

def test_the_versioned_dll_is_found_not_the_abi_forwarder(tmp_path):
    """python3.dll is the stable-ABI forwarder; Reaper needs the real
    versioned library."""
    (tmp_path / "python3.dll").write_text("forwarder")
    (tmp_path / "python310.dll").write_text("real")

    assert reapersetup.find_dll(tmp_path).name == "python310.dll"


def test_no_dll_is_reported_rather_than_guessed(tmp_path):
    assert reapersetup.find_dll(tmp_path) is None


def test_the_server_script_path_is_inside_the_unpacked_runtime(tmp_path):
    """Reaper stores this path and runs it at startup, so it must be
    somewhere permanent - not PyInstaller's temp directory, which is
    deleted when DAWBridge closes."""
    script = reapersetup.server_script(tmp_path)

    assert script.is_relative_to(tmp_path)
    assert script.name == "activate_reapy_server.py"


def test_the_runtime_lives_somewhere_that_outlives_the_exe(monkeypatch, tmp_path):
    """Someone runs DAWBridge from Downloads and tidies up later. An
    interpreter that vanished with it would leave reaper.ini naming a
    DLL that no longer exists."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert reapersetup.runtime_root().is_relative_to(tmp_path)


# ---- unpacking ---------------------------------------------------------

def test_unpacking_lays_out_a_usable_runtime(tmp_path):
    archive = _fake_runtime_zip(tmp_path)
    runtime = tmp_path / "runtime"

    dll = reapersetup.unpack_runtime(archive, runtime)

    assert dll == runtime / "python310.dll"
    assert reapersetup.is_unpacked(runtime)
    assert reapersetup.server_script(runtime).exists()


def test_a_runtime_missing_its_server_script_is_not_considered_unpacked(tmp_path):
    """Both files get used. A folder that exists and works for nothing is
    the failure worth catching."""
    archive = _fake_runtime_zip(tmp_path, with_server=False)
    runtime = tmp_path / "runtime"
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(runtime)

    assert not reapersetup.is_unpacked(runtime)


def test_unpacking_twice_does_not_redo_the_work(tmp_path):
    archive = _fake_runtime_zip(tmp_path)
    runtime = tmp_path / "runtime"
    reapersetup.unpack_runtime(archive, runtime)
    (runtime / "marker").write_text("still here")

    reapersetup.unpack_runtime(archive, runtime)

    assert (runtime / "marker").exists()


def test_a_bundle_with_no_interpreter_is_refused_and_leaves_nothing_behind(tmp_path):
    """Better a clear packaging error than a runtime directory that
    exists, satisfies a later existence check, and can't run anything."""
    archive = _fake_runtime_zip(tmp_path, dll="")
    runtime = tmp_path / "runtime"

    with pytest.raises(RuntimeError, match="packaging fault"):
        reapersetup.unpack_runtime(archive, runtime)
    assert not runtime.exists()


def test_an_interrupted_unpack_does_not_leave_a_usable_looking_runtime(tmp_path):
    """Staged in a sibling directory and moved into place, so a full disk
    or a killed process can't produce a half-populated runtime."""
    archive = _fake_runtime_zip(tmp_path, dll="")
    runtime = tmp_path / "runtime"

    with pytest.raises(RuntimeError):
        reapersetup.unpack_runtime(archive, runtime)

    assert not reapersetup.is_unpacked(runtime)
    assert not runtime.with_name(runtime.name + ".unpacking").exists()


# ---- reading Reaper's own config --------------------------------------

def test_reapers_settings_folder_is_only_claimed_when_it_holds_a_config(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    (tmp_path / "REAPER").mkdir()

    assert reapersetup.resource_dir() is None

    (tmp_path / "REAPER" / "reaper.ini").write_text("[reaper]\n")
    assert reapersetup.resource_dir() == tmp_path / "REAPER"


def test_state_is_described_without_pretending_reaper_was_found(tmp_path):
    lines = "\n".join(reapersetup.describe_state(None, tmp_path / "nothing"))

    assert "wasn't found" in lines
    assert "not unpacked yet" in lines


# ---- verification ------------------------------------------------------

def _write_ini(tmp_path: Path, **values) -> Path:
    resource = tmp_path / "REAPER"
    resource.mkdir(exist_ok=True)
    body = "[reaper]\n" + "".join(f"{k}={v}\n" for k, v in values.items())
    (resource / "reaper.ini").write_text(body, encoding="utf-8")
    return resource


@needs_reapy
def test_a_config_reaper_reverted_is_reported_not_celebrated(tmp_path):
    """Reaper rewrites reaper.ini from memory when it quits, so edits
    made while it is open are silently undone. Reporting success there
    would send someone away believing they're set up."""
    resource = _write_ini(tmp_path, reascript="1", pythonlibdll64="python39.dll")

    problem = reapersetup.verify(resource, tmp_path / "python310.dll")

    assert problem is not None and "still open" in problem


@needs_reapy
def test_reascript_left_switched_off_is_reported(tmp_path):
    resource = _write_ini(tmp_path, reascript="0", pythonlibdll64="python310.dll")

    assert reapersetup.verify(resource, tmp_path / "python310.dll") is not None


@needs_reapy
def test_a_config_that_took_verifies_clean(tmp_path):
    resource = _write_ini(tmp_path, reascript="1", pythonlibdll64="python310.dll")

    assert reapersetup.verify(resource, tmp_path / "python310.dll") is None


# ---- the whole configure step, against a fake Reaper install ----------

@needs_reapy
def test_configure_writes_everything_reaper_needs(tmp_path):
    """The real integration point, exercised without a live Reaper.

    reapy's helpers are doing the reaper-kb.ini and web-interface work,
    so what this pins down is that they are called with OUR interpreter
    and OUR script path - the whole point of not using
    reapy.configure_reaper(), which finds the running interpreter's DLL
    and inside a frozen .exe that is a temp path that stops existing the
    moment DAWBridge closes.
    """
    resource = tmp_path / "REAPER"
    resource.mkdir()
    (resource / "reaper.ini").write_text("[reaper]\ncsurf_cnt=0\n", encoding="utf-8")
    (resource / "reaper-kb.ini").write_text("", encoding="utf-8")

    runtime = tmp_path / "runtime"
    reapersetup.unpack_runtime(_fake_runtime_zip(tmp_path), runtime)
    dll = reapersetup.find_dll(runtime)

    steps = reapersetup.configure(resource, dll, reapersetup.server_script(runtime))

    ini = (resource / "reaper.ini").read_text(encoding="utf-8")
    assert "reascript=1" in ini.replace(" ", "")
    assert str(runtime) in ini            # our interpreter, not the machine's
    assert "python310.dll" in ini
    assert "HTTP 0 2307" in ini           # the transport reapy actually uses
    # The bridge script is registered by path, and that path has to be
    # the permanent one - not PyInstaller's temp directory.
    assert str(runtime) in (resource / "reaper-kb.ini").read_text(encoding="utf-8")
    assert len(steps) == 3


@needs_reapy
def test_configure_keeps_a_backup_of_settings_it_edits(tmp_path):
    """reaper.ini holds preferences someone may have spent years on."""
    resource = tmp_path / "REAPER"
    resource.mkdir()
    (resource / "reaper.ini").write_text("[reaper]\ncsurf_cnt=0\nmycherished=1\n",
                                         encoding="utf-8")
    (resource / "reaper-kb.ini").write_text("", encoding="utf-8")
    runtime = tmp_path / "runtime"
    reapersetup.unpack_runtime(_fake_runtime_zip(tmp_path), runtime)

    reapersetup.configure(resource, reapersetup.find_dll(runtime),
                          reapersetup.server_script(runtime))

    assert (resource / "reaper.ini.bak").exists()
    assert "mycherished=1" in (resource / "reaper.ini").read_text(encoding="utf-8")


# ---- the fused-entry corruption ---------------------------------------

def _kb(tmp_path: Path, body: str) -> Path:
    resource = tmp_path / "REAPER"
    resource.mkdir(exist_ok=True)
    (resource / "reaper-kb.ini").write_text(body, encoding="utf-8")
    return resource


_ENTRY_A = r'SCR 4 0 RSaaa "Custom: activate_reapy_server.py" C:\Py310\reapy\activate_reapy_server.py'
_ENTRY_B = r'SCR 4 0 RSbbb "Custom: activate_reapy_server.py" C:\DAWBridge\reapy\activate_reapy_server.py'


def test_fused_entries_are_split_back_apart(tmp_path):
    """The real failure, byte for byte. reapy appends with no trailing
    newline, so a second entry lands on the end of the first and Reaper
    can parse neither - DAWBridge then triggers an action id Reaper has
    never heard of, nothing runs, and reapy waits forever for a server
    that will never start. Seen on a real install: 400 bytes, two
    entries, zero newlines.
    """
    resource = _kb(tmp_path, _ENTRY_A + _ENTRY_B)

    assert reapersetup.repair_script_list(resource) is True

    lines = (resource / "reaper-kb.ini").read_text(encoding="utf-8").splitlines()
    assert lines == [_ENTRY_A, _ENTRY_B]


def test_repair_leaves_a_trailing_newline_so_the_next_append_is_safe(tmp_path):
    """Repairing damage already done is half of it; the other half is
    that reapy's next append must not recreate it."""
    resource = _kb(tmp_path, _ENTRY_A)

    reapersetup.repair_script_list(resource)

    assert (resource / "reaper-kb.ini").read_text(encoding="utf-8").endswith(chr(10))


def test_a_healthy_file_is_left_completely_alone(tmp_path):
    """This file holds key bindings someone may have spent years on. A
    repair that 'tidied' anything would be worse than the bug."""
    body = _ENTRY_A + chr(10) + _ENTRY_B + chr(10)
    resource = _kb(tmp_path, body)

    assert reapersetup.repair_script_list(resource) is False
    assert (resource / "reaper-kb.ini").read_text(encoding="utf-8") == body


def test_repair_keeps_a_backup_of_what_it_replaced(tmp_path):
    resource = _kb(tmp_path, _ENTRY_A + _ENTRY_B)

    reapersetup.repair_script_list(resource)

    assert (resource / "reaper-kb.ini.bak").read_text(encoding="utf-8") == _ENTRY_A + _ENTRY_B


def test_a_missing_script_list_is_not_an_error(tmp_path):
    resource = tmp_path / "REAPER"
    resource.mkdir()
    assert reapersetup.repair_script_list(resource) is False


@needs_reapy
def test_configure_repairs_before_adding_its_own_entry(tmp_path):
    """The wiring. Without this ordering, configure() adds a third entry
    onto the end of an already-fused line and makes it worse."""
    resource = tmp_path / "REAPER"
    resource.mkdir()
    (resource / "reaper.ini").write_text("[reaper]\ncsurf_cnt=0\n", encoding="utf-8")
    (resource / "reaper-kb.ini").write_text(_ENTRY_A + _ENTRY_B, encoding="utf-8")

    runtime = tmp_path / "runtime"
    reapersetup.unpack_runtime(_fake_runtime_zip(tmp_path), runtime)
    steps = reapersetup.configure(resource, reapersetup.find_dll(runtime),
                                  reapersetup.server_script(runtime))

    body = (resource / "reaper-kb.ini").read_text(encoding="utf-8")
    assert ".pySCR" not in body           # nothing fused, old or new
    entries = [l for l in body.splitlines() if l.startswith("SCR 4 0 ")]
    assert len(entries) == 3              # the two originals plus ours
    assert any("repaired" in s for s in steps)


# ---- never take away a Python that already works ----------------------

def _installed_python(tmp_path: Path, name: str = "python310.dll") -> Path:
    """Stands in for a Python the user installed themselves."""
    where = tmp_path / "SystemPython"
    where.mkdir(exist_ok=True)
    (where / name).write_text("dll")
    return where


def _reaper_with_python(tmp_path: Path, python_dir: Path) -> Path:
    resource = tmp_path / "REAPER"
    resource.mkdir(exist_ok=True)
    (resource / "reaper.ini").write_text(
        "[reaper]\ncsurf_cnt=0\nreascript=1\n"
        f"pythonlibpath64={python_dir}\npythonlibdll64=python310.dll\n",
        encoding="utf-8")
    (resource / "reaper-kb.ini").write_text("", encoding="utf-8")
    return resource


def test_a_working_python_is_detected(tmp_path):
    resource = _reaper_with_python(tmp_path, _installed_python(tmp_path))

    found = reapersetup.existing_python(resource)

    assert found is not None and found[1] == "python310.dll"


def test_a_python_that_was_uninstalled_does_not_count(tmp_path):
    """A path left behind by a Python that is no longer there is not
    something worth preserving."""
    resource = _reaper_with_python(tmp_path, tmp_path / "GoneAway")

    assert reapersetup.existing_python(resource) is None


@needs_reapy
def test_configure_refuses_to_replace_a_working_python(tmp_path):
    """The regression that broke a real machine. Reaper had a working
    Python and reapy setup for months; setup overwrote pythonlibpath64
    with the bundled interpreter and it stopped working. A convenience
    that damages the people who never needed it is worse than no
    convenience.
    """
    resource = _reaper_with_python(tmp_path, _installed_python(tmp_path))
    runtime = tmp_path / "runtime"
    reapersetup.unpack_runtime(_fake_runtime_zip(tmp_path), runtime)

    with pytest.raises(reapersetup.PythonAlreadyWorking):
        reapersetup.configure(resource, reapersetup.find_dll(runtime),
                              reapersetup.server_script(runtime))

    # And it really left it alone, rather than refusing after writing.
    body = (resource / "reaper.ini").read_text(encoding="utf-8")
    assert "SystemPython" in body
    assert str(runtime) not in body


@needs_reapy
def test_a_machine_with_no_python_is_still_set_up(tmp_path):
    """The case the bundled interpreter was actually built for."""
    resource = tmp_path / "REAPER"
    resource.mkdir()
    (resource / "reaper.ini").write_text("[reaper]\ncsurf_cnt=0\n", encoding="utf-8")
    (resource / "reaper-kb.ini").write_text("", encoding="utf-8")
    runtime = tmp_path / "runtime"
    reapersetup.unpack_runtime(_fake_runtime_zip(tmp_path), runtime)

    reapersetup.configure(resource, reapersetup.find_dll(runtime),
                          reapersetup.server_script(runtime))

    assert str(runtime) in (resource / "reaper.ini").read_text(encoding="utf-8")


@needs_reapy
def test_re_running_setup_on_its_own_runtime_is_fine(tmp_path, monkeypatch):
    """Refusing must not lock DAWBridge out of the config it wrote
    itself, or setup becomes a one-shot that can never be repeated."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    runtime = reapersetup.runtime_root()
    reapersetup.unpack_runtime(_fake_runtime_zip(tmp_path), runtime)
    resource = _reaper_with_python(tmp_path, runtime)

    reapersetup.configure(resource, reapersetup.find_dll(runtime),
                          reapersetup.server_script(runtime))  # must not raise


@needs_reapy
def test_the_user_can_still_override_deliberately(tmp_path):
    resource = _reaper_with_python(tmp_path, _installed_python(tmp_path))
    runtime = tmp_path / "runtime"
    reapersetup.unpack_runtime(_fake_runtime_zip(tmp_path), runtime)

    reapersetup.configure(resource, reapersetup.find_dll(runtime),
                          reapersetup.server_script(runtime),
                          replace_existing_python=True)

    assert str(runtime) in (resource / "reaper.ini").read_text(encoding="utf-8")


def test_an_existing_python_is_reported_as_nothing_to_do(tmp_path):
    """The readout must say this BEFORE anything is pressed. Someone who
    reads "Reaper isn't set up" will press the button that breaks them -
    which is exactly how a working machine got broken.
    """
    resource = _reaper_with_python(tmp_path, _installed_python(tmp_path))

    lines = chr(10).join(reapersetup.describe_state(resource, tmp_path / "nothing"))

    assert "already uses its own" in lines
    assert "will not replace a working Python" in lines


def test_a_reaper_that_was_never_found_claims_nothing_about_its_python(tmp_path):
    lines = chr(10).join(reapersetup.describe_state(None, tmp_path / "nothing"))

    assert "wasn't found" in lines
    assert "has none configured" not in lines

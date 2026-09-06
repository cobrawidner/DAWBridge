"""Throwaway manual test runner - pytest isn't installable in this sandbox
(no network access to PyPI), so this substitutes for `pytest -q` to verify
the dawbridge test suite's logic actually passes. Not part of the deliverable.
"""
import inspect
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, "dawbridge")
sys.path.insert(0, "dawbridge/tests")

import test_model, test_tagging, test_sync, test_backend_mock, test_store

modules = [test_model, test_tagging, test_sync, test_backend_mock, test_store]

passed = 0
failed = 0

for mod in modules:
    for name, fn in inspect.getmembers(mod, inspect.isfunction):
        if not name.startswith("test_"):
            continue
        sig = inspect.signature(fn)
        kwargs = {}
        tmpdir_ctx = None
        if "tmp_path" in sig.parameters:
            tmpdir_ctx = tempfile.TemporaryDirectory()
            kwargs["tmp_path"] = Path(tmpdir_ctx.name)
        try:
            fn(**kwargs)
            passed += 1
            print(f"PASS {mod.__name__}.{name}")
        except Exception:
            failed += 1
            print(f"FAIL {mod.__name__}.{name}")
            traceback.print_exc()
        finally:
            if tmpdir_ctx:
                tmpdir_ctx.cleanup()

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

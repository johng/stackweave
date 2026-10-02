"""An extension built without both migration patches says so.

Fibers migrate between hubs whenever there are two, and on an interpreter
without both patches (src/patches/) that can crash under load -- a SIGSEGV
that looks like a runtime bug.  The interpreter can't be checked at runtime,
but the build can: stackweave_c.migration_patched is 0 when it lacked either
feature, and mn_init then warns once when it starts 2+ hubs.  A patched build
(what CI runs) never warns.
"""
import os
import pathlib
import subprocess
import sys

import pytest

from adv_util import needs_free_threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
WARNING = "built against a CPython without the migration patches"

pytestmark = pytest.mark.skipif(not needs_free_threading(),
                                reason="M:N needs free-threaded CPython")


def _stderr(code):
    p = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                       env=dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src"),
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-2000:]
    return p.stderr


def test_two_hubs_warn_once_iff_the_build_lacks_the_patches():
    import stackweave_c
    err = _stderr("import stackweave\n"
                  "for _ in range(2):\n"
                  "    stackweave.run(2, lambda: None)\n")
    assert err.count(WARNING) == (0 if stackweave_c.migration_patched else 1), err


def test_one_hub_never_warns():
    assert WARNING not in _stderr("import stackweave\n"
                                  "stackweave.run(1, lambda: None)\n")

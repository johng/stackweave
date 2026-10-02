"""A build that can't run migration safely says so.

Fibers migrate between hubs whenever there are two, and without both patches
(src/patches/) that can crash under load -- a SIGSEGV that looks like a
runtime bug.  stackweave_c.migration_patched is 0 when the extension was
built without either feature, and mn_init then warns once when it starts 2+
hubs.  Nothing in the ABI tells the patched thread-state layout from the stock
one, so `import stackweave` also warns when the extension's patch set differs
from the interpreter's (a stale build from the other interpreter, or -D flags
forced on a stock one).  A patched build on its own interpreter (what CI
runs) says nothing.
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


MISMATCH = "the two disagree about the thread-state layout"


def test_import_is_quiet_when_the_build_matches_the_interpreter():
    assert MISMATCH not in _stderr("import stackweave\n")


@pytest.mark.parametrize("built", [0, 1])
@pytest.mark.parametrize("running", [False, True])
def test_a_build_for_another_patch_set_is_reported(monkeypatch, capsys, built, running):
    import stackweave_c
    from stackweave import runtime
    monkeypatch.setattr(stackweave_c, "migration_patched", built)
    monkeypatch.setattr(runtime, "_interpreter_migration_patched", lambda: running)
    runtime._check_migration_build()
    assert (MISMATCH in capsys.readouterr().err) == (bool(built) != running)


@pytest.mark.parametrize("config, patched", [
    ({"Py_TSTATE_ALLOC_HOME": 1, "Py_TSTATE_EXEC_HOME": 1}, True),
    ({"CONFIGURE_CPPFLAGS": "-DPy_TSTATE_ALLOC_HOME -DPy_TSTATE_EXEC_HOME=1"}, True),
    ({"Py_TSTATE_ALLOC_HOME": 1}, False),
    ({"CONFIGURE_CPPFLAGS": "-DPy_TSTATE_EXEC_HOME"}, False),
    ({}, False),
])
def test_the_interpreter_check_reads_pyconfig_and_configure_cppflags(monkeypatch, config, patched):
    import sysconfig
    from stackweave import runtime
    monkeypatch.setattr(sysconfig, "get_config_var", config.get)
    assert runtime._interpreter_migration_patched() is patched

"""pip must refuse to install stackweave onto an unpatched CPython.

pip can't tell the patched free-threaded interpreter from a stock one -- both
advertise the cp3NNt wheel tag -- so setup.py gates the two commands pip drives
(bdist_wheel for `pip install`, editable_wheel for `pip install -e`) on the
migration-patch witnesses.  `setup.py build_ext --inplace` stays ungated; the
rest of the suite runs on the extension it builds, so it covers that half.

The refusal test is skipped when the interpreter's installed pyconfig.h defines
both patch features: there the gate passes and the command would go on to
compile the whole extension.  The flag env vars are cleared so the interpreter
alone decides.
"""
import os
import pathlib
import subprocess
import sys
import sysconfig

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

needs_unpatched = pytest.mark.skipif(
    all(sysconfig.get_config_var(f) for f in ("Py_TSTATE_ALLOC_HOME", "Py_TSTATE_EXEC_HOME")),
    reason="patched interpreter: the gate passes and the build would compile")


def _setup(tmp_path, *args):
    env = dict(os.environ)
    for k in ("STACKWEAVE_ALLOW_STOCK_CPYTHON", "STACKWEAVE_EXTRA_CFLAGS", "CFLAGS", "CPPFLAGS"):
        env.pop(k, None)
    return subprocess.run(
        [sys.executable, "setup.py", *args, "--dist-dir", str(tmp_path / "dist")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


@needs_unpatched
@pytest.mark.parametrize("command", ["bdist_wheel", "editable_wheel"])
def test_wheel_build_refused_on_unpatched_cpython(tmp_path, command):
    r = _setup(tmp_path, command)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "installs only onto a free-threaded CPython" in r.stderr, r.stderr
    assert not (tmp_path / "dist").exists()


def _gate(monkeypatch, tmp_path, headers):
    """setup.py's patched_cpython_problems() against a fake patched interpreter
    whose include dir holds `headers` ({relpath: text})."""
    import contextlib
    import io
    import runpy
    import sysconfig

    include = tmp_path / "include"
    for rel, text in headers.items():
        (include / rel).parent.mkdir(parents=True, exist_ok=True)
        (include / rel).write_text(text)
    pyconfig = tmp_path / "pyconfig.h"
    pyconfig.write_text("#define Py_TSTATE_ALLOC_HOME 1\n"
                        "#define Py_TSTATE_EXEC_HOME 1\n")
    config = {"Py_GIL_DISABLED": 1, "Py_TSTATE_ALLOC_HOME": 1,
              "Py_TSTATE_EXEC_HOME": 1, "CONFIGURE_CPPFLAGS": ""}

    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(sys, "argv", ["setup.py", "--name"])
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        ns = runpy.run_path(str(ROOT / "setup.py"), run_name="stackweave_setup")
    monkeypatch.setattr(sysconfig, "get_config_var", config.get)
    monkeypatch.setattr(sysconfig, "get_path", lambda *a, **k: str(include))
    monkeypatch.setattr(sysconfig, "get_config_h_filename", lambda: str(pyconfig))
    return ns["patched_cpython_problems"]()


# 3.13/3.14 declare _Py_ThreadId -- and so the exec-home witness -- in object.h;
# 3.15 moved it to cpython/object.h.  Either is a patched interpreter.
@pytest.mark.parametrize("exec_header", ["object.h", "cpython/object.h"])
def test_gate_accepts_exec_home_witness_in_either_header(monkeypatch, tmp_path,
                                                         exec_header):
    problems = _gate(monkeypatch, tmp_path, {
        "internal/pycore_tstate.h": "_PyThreadStateImpl_AllocHome",
        exec_header: "_Py_TID_ASM",
    })
    assert problems == []


def test_gate_refuses_when_no_header_has_the_exec_home_witness(monkeypatch, tmp_path):
    problems = _gate(monkeypatch, tmp_path, {
        "internal/pycore_tstate.h": "_PyThreadStateImpl_AllocHome",
        "object.h": "", "cpython/object.h": "",
    })
    assert len(problems) == 1 and "Py_TSTATE_EXEC_HOME" in problems[0], problems

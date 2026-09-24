"""pip must refuse to install stackweave onto an unpatched CPython.

pip can't tell the patched free-threaded interpreter from a stock one -- both
advertise the cp3NNt wheel tag -- so setup.py gates the two commands pip drives
(bdist_wheel for `pip install`, editable_wheel for `pip install -e`) on the
migration-patch witnesses.  `setup.py build_ext --inplace` stays ungated; the
rest of the suite runs on the extension it builds, so it covers that half.

Skipped on a patched interpreter: there the gate passes and the command would go
on to compile the whole extension.
"""
import os
import pathlib
import subprocess
import sys

import pytest

import stackweave

ROOT = pathlib.Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    stackweave.migration_available(),
    reason="patched interpreter: the gate passes and the build would compile")


def _setup(tmp_path, *args):
    env = dict(os.environ)
    env.pop("STACKWEAVE_ALLOW_STOCK_CPYTHON", None)
    return subprocess.run(
        [sys.executable, "setup.py", *args, "--dist-dir", str(tmp_path / "dist")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("command", ["bdist_wheel", "editable_wheel"])
def test_wheel_build_refused_on_unpatched_cpython(tmp_path, command):
    r = _setup(tmp_path, command)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "installs only onto a free-threaded CPython" in r.stderr, r.stderr
    assert not (tmp_path / "dist").exists()

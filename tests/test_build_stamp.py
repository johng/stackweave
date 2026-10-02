"""`setup.py build_ext` rebuilds everything when the interpreter or the build
flags change.

build_ext on its own skips an extension entirely whenever the built extension
is newer than its sources.  A stock and a patched interpreter of one version
share every build path (both are cp3NNt), so building with one after the other
built nothing and copied the first one's extension back into src/, where its
_PyThreadStateImpl layout disagreed with the second interpreter's and the first
M:N test segfaulted.  setup.py now leaves a stamp of what each build was for in
build_temp and forces a full rebuild when it differs.

These run the real runloom_build_ext with the compile step stubbed out, and
read off whether it asked for a full rebuild.  Each _load() is a fresh
setup.py, as a new `python setup.py` process would see it.
"""
import contextlib
import io
import json
import pathlib
import runpy
import sys
import sysconfig

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(monkeypatch):
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(sys, "argv", ["setup.py", "--name"])
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        return runpy.run_path(str(ROOT / "setup.py"), run_name="stackweave_setup")


@pytest.fixture
def setup_ns(monkeypatch):
    return _load(monkeypatch)


def _build(ns, monkeypatch, build_temp, compile_stub=None, force=False,
           build_lib=None):
    """One build_ext run in build_temp with the compile step replaced by
    compile_stub(cmd); returns (forced, stdout)."""
    from setuptools.dist import Distribution
    forced = []

    def run(self):
        if compile_stub is not None:
            compile_stub(self)
        forced.append(bool(self.force))

    monkeypatch.setattr(ns["_build_ext"], "run", run)
    dist = Distribution({"name": "stackweave", "ext_modules": [ns["ext"]],
                         "cmdclass": ns["cmdclass"]})
    cmd = ns["runloom_build_ext"](dist)
    cmd.force = force
    cmd.ensure_finalized()
    cmd.build_temp = str(build_temp)
    if build_lib is not None:
        cmd.build_lib = str(build_lib)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cmd.run()
    return forced[-1], out.getvalue()


def _stamp(build_temp):
    return json.loads((build_temp / "stackweave-build.json").read_text())


def test_a_first_build_compiles_everything_and_a_repeat_does_not(setup_ns, monkeypatch, tmp_path):
    forced, _ = _build(setup_ns, monkeypatch, tmp_path)
    assert forced
    assert _stamp(tmp_path)["asm_failed"] is False
    forced, out = _build(_load(monkeypatch), monkeypatch, tmp_path)
    assert not forced, out


def test_another_interpreter_recompiles_everything(setup_ns, monkeypatch, tmp_path):
    _build(setup_ns, monkeypatch, tmp_path)
    other = tmp_path / "other-include"
    other.mkdir()
    real_get_path = sysconfig.get_path
    monkeypatch.setattr(sysconfig, "get_path",
                        lambda name, *a, **k: str(other) if name == "include"
                        else real_get_path(name, *a, **k))
    forced, out = _build(_load(monkeypatch), monkeypatch, tmp_path)
    assert forced
    assert "the interpreter or the build flags changed" in out, out


def test_other_compile_flags_recompile_everything(setup_ns, monkeypatch, tmp_path):
    _build(setup_ns, monkeypatch, tmp_path)
    ns = _load(monkeypatch)
    ext = ns["ext"]
    monkeypatch.setattr(ext, "extra_compile_args",
                        list(ext.extra_compile_args) + ["-DSTACKWEAVE_STAMP_TEST"])
    forced, _ = _build(ns, monkeypatch, tmp_path)
    assert forced


def test_another_build_lib_recompiles_everything(setup_ns, monkeypatch, tmp_path):
    # A build into another --build-lib must not stamp the default one as
    # current: a later default build would then copy that one's stale .so.
    _build(setup_ns, monkeypatch, tmp_path)
    forced, _ = _build(_load(monkeypatch), monkeypatch, tmp_path,
                       build_lib=tmp_path / "altlib")
    assert forced
    forced, _ = _build(_load(monkeypatch), monkeypatch, tmp_path)
    assert forced


def test_a_failed_build_leaves_the_old_stamp(setup_ns, monkeypatch, tmp_path):
    _build(setup_ns, monkeypatch, tmp_path)
    before = _stamp(tmp_path)
    ns = _load(monkeypatch)
    monkeypatch.setattr(ns["ext"], "extra_compile_args",
                        list(ns["ext"].extra_compile_args) + ["-DSTACKWEAVE_STAMP_TEST"])

    def broken(cmd):
        raise RuntimeError("compiler failed")

    with pytest.raises(RuntimeError):
        _build(ns, monkeypatch, tmp_path, compile_stub=broken)
    assert _stamp(tmp_path) == before


def _rejects_asm(cmd):
    if any(s.endswith((".S", ".s")) for e in cmd.extensions for s in e.sources):
        raise RuntimeError("assembler rejected the .S")


def test_an_asm_fallback_is_remembered(setup_ns, monkeypatch, tmp_path):
    if not any(s.endswith((".S", ".s")) for s in setup_ns["ext"].sources):
        pytest.skip("this platform builds without asm")
    forced, out = _build(setup_ns, monkeypatch, tmp_path, compile_stub=_rejects_asm)
    assert forced and "retrying with ucontext" in out, out
    assert _stamp(tmp_path)["asm_failed"] is True
    # The next build starts with ucontext, and says so: no failed asm, no
    # rebuild.
    forced, out = _build(_load(monkeypatch), monkeypatch, tmp_path,
                         compile_stub=_rejects_asm)
    assert not forced and "retrying" not in out, out
    assert "the asm failed last time" in out, out
    # --force tries the asm again.
    forced, out = _build(_load(monkeypatch), monkeypatch, tmp_path,
                         compile_stub=_rejects_asm, force=True)
    assert forced and "retrying with ucontext" in out, out


def test_an_asm_fallback_is_not_kept_across_a_toolchain_change(setup_ns, monkeypatch, tmp_path):
    if not any(s.endswith((".S", ".s")) for s in setup_ns["ext"].sources):
        pytest.skip("this platform builds without asm")
    _build(setup_ns, monkeypatch, tmp_path, compile_stub=_rejects_asm)
    assert _stamp(tmp_path)["asm_failed"] is True
    # Another compiler may take the asm: try it rather than stay on ucontext.
    monkeypatch.setenv("CC", "another-cc")
    tried = []

    def accepts_asm(cmd):
        tried.append(any(s.endswith((".S", ".s"))
                         for e in cmd.extensions for s in e.sources))

    forced, out = _build(_load(monkeypatch), monkeypatch, tmp_path,
                         compile_stub=accepts_asm)
    assert forced and tried == [True], (tried, out)
    assert "the asm failed last time" not in out, out
    assert _stamp(tmp_path)["asm_failed"] is False

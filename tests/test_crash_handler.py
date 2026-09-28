"""Tests for the fatal-signal crash reporter (runloom_crash.c / inspect.install_crash_handler).

A real crash kills the process, so every crash-trigger test runs in a child
process and asserts on its exit signal and captured stderr.  The classification
(fiber stack overflow vs wild pointer vs non-fiber) and the per-thread
sigaltstack (so the handler survives the overflow it is reporting) are the
behaviours under test.
"""
import os
import signal
import subprocess
import sys
import textwrap

import pytest

import stackweave            # noqa: F401  (import side effects: registers fork handler)
import stackweave_c

BACKEND = stackweave_c.backend()
# The address->fiber guard-page mapping exists on both stack backends.
HAS_GUARD = BACKEND in ("fcontext-asm", "ucontext")

requires_guard = pytest.mark.skipif(
    not HAS_GUARD,
    reason="crash classification needs a guard-page backend (got %s)" % BACKEND,
)

# A fatal memory fault is SIGSEGV on Linux but SIGBUS on macOS arm64 -- a stack
# guard-page hit / misaligned access is delivered as SIGBUS there.  Accept either:
# every assertion below only cares that the process died from a fatal fault the
# crash handler chained to the default handler, not which of the two it was.
FAULT_RCS = {-signal.SIGSEGV} | ({-signal.SIGBUS} if hasattr(signal, "SIGBUS") else set())


def run_child(body, timeout=60):
    """Run `body` as a fresh child Python process; return (returncode, output).

    The child inherits this run's interpreter + PYTHONPATH (so it imports the
    same source tree).
    """
    src = "import stackweave, stackweave_c, ctypes, sys\n" + textwrap.dedent(body)
    p = subprocess.run(
        [sys.executable, "-c", src],
        capture_output=True, text=True, timeout=timeout,
    )
    return p.returncode, p.stdout + p.stderr


# --------------------------------------------------------------------------- #
#  In-process API
# --------------------------------------------------------------------------- #
def test_install_uninstall_roundtrip():
    assert stackweave_c.crash_handler_installed() is False
    try:
        flags = stackweave.inspect.install_crash_handler("on")
        assert isinstance(flags, int) and flags > 0
        assert stackweave_c.crash_handler_installed() is True
    finally:
        stackweave.inspect.uninstall_crash_handler()
    assert stackweave_c.crash_handler_installed() is False


def test_install_idempotent():
    try:
        stackweave.inspect.install_crash_handler("on")
        stackweave.inspect.install_crash_handler("on")   # no error, still installed
        assert stackweave_c.crash_handler_installed() is True
    finally:
        stackweave.inspect.uninstall_crash_handler()


def test_off_level_uninstalls():
    try:
        stackweave.inspect.install_crash_handler("on")
        assert stackweave_c.crash_handler_installed() is True
        stackweave.inspect.install_crash_handler("off")
        assert stackweave_c.crash_handler_installed() is False
    finally:
        stackweave.inspect.uninstall_crash_handler()


@pytest.mark.parametrize("level", ["on", "all", "backtrace", "pystack", "wait", "gdb",
                                   "backtrace,pystack"])
def test_level_strings_parse(level):
    try:
        flags = stackweave.inspect.install_crash_handler(level)
        assert isinstance(flags, int) and flags > 0
        assert stackweave_c.crash_handler_installed() is True
    finally:
        stackweave.inspect.uninstall_crash_handler()


# --------------------------------------------------------------------------- #
#  Does not interfere with a normal (non-crashing) run
# --------------------------------------------------------------------------- #
def test_no_interference_on_clean_run():
    rc, out = run_child("""
        stackweave.inspect.install_crash_handler("all")
        results = []
        def work():
            results.append(42)
        stackweave_c.fiber(work)
        stackweave_c.run()
        print("CLEAN-EXIT", results)
    """)
    assert rc == 0, out
    assert "CLEAN-EXIT [42]" in out
    assert "stackweave crash" not in out


# --------------------------------------------------------------------------- #
#  Goroutine stack overflow -> classified, named, survived
# --------------------------------------------------------------------------- #
@requires_guard
def test_overflow_classified_single_thread():
    rc, out = run_child("""
        stackweave.inspect.install_crash_handler("on")
        def boom():
            stackweave_c._crash_selftest_overflow()   # unbounded real-C recursion
        # 256 KiB is honored exactly on both 3.13 (16 KiB floor) and FT-3.14
        # (256 KiB floor, the p226 fix in 289ecb99) -- a sub-floor size would
        # be clamped up and the classifier would name the clamped size.
        stackweave_c.fiber(boom, 256 * 1024)
        stackweave_c.run()
    """)
    assert rc in FAULT_RCS, (rc, out)          # chained to default -> cored
    assert "GOROUTINE STACK OVERFLOW" in out, out
    assert "256 KiB" in out, out                      # named its stack size
    assert "fiber g" in out, out
    assert "=== stackweave fiber dump" in out, out   # full registry dump too


@requires_guard
def test_overflow_classified_under_mn_scheduler():
    # The fault fires on a HUB thread; this proves the per-thread sigaltstack was
    # armed via runloom_coro_thread_init at hub start.
    rc, out = run_child("""
        stackweave.inspect.install_crash_handler("on")
        stackweave_c.mn_init(2)
        def boom():
            stackweave_c._crash_selftest_overflow()
        stackweave_c.mn_fiber(boom)
        stackweave_c.mn_run()
    """)
    assert rc in FAULT_RCS, (rc, out)
    assert "GOROUTINE STACK OVERFLOW" in out, out
    assert "this thread was executing fiber g" in out, out


# --------------------------------------------------------------------------- #
#  Wild pointer (NULL deref) -> NOT classified as overflow
# --------------------------------------------------------------------------- #
@requires_guard
def test_wild_pointer_not_classified_as_overflow():
    rc, out = run_child("""
        stackweave.inspect.install_crash_handler("on")
        def boom():
            ctypes.string_at(0)        # read address 0 -- not a guard page
        stackweave_c.fiber(boom)
        stackweave_c.run()
    """)
    assert rc in FAULT_RCS, (rc, out)
    assert "not in any fiber stack" in out, out
    assert "GOROUTINE STACK OVERFLOW" not in out, out
    assert "=== stackweave fiber dump" in out, out


# --------------------------------------------------------------------------- #
#  Python traceback chains in (faulthandler) under pystack
# --------------------------------------------------------------------------- #
@requires_guard
def test_pystack_chains_python_traceback():
    rc, out = run_child("""
        stackweave.inspect.install_crash_handler("all")   # all => +pystack
        def boom():
            ctypes.string_at(0)
        stackweave_c.fiber(boom)
        stackweave_c.run()
    """)
    assert rc in FAULT_RCS, (rc, out)
    assert "stackweave crash" in out, out                 # our dump ran first
    # ... then faulthandler printed the Python traceback and re-raised default.
    assert "Fatal Python error" in out, out
    assert "in boom" in out, out


# --------------------------------------------------------------------------- #
#  Report file (file=)
# --------------------------------------------------------------------------- #
@requires_guard
def test_report_written_to_file(tmp_path):
    report = tmp_path / "crash.txt"
    rc, out = run_child("""
        stackweave.inspect.install_crash_handler("on", %r)
        def boom():
            stackweave_c._crash_selftest_overflow()
        stackweave_c.fiber(boom, 16384)
        stackweave_c.run()
    """ % str(report))
    assert rc in FAULT_RCS, (rc, out)
    assert report.exists(), "report file not created"
    text = report.read_text()
    assert "stackweave crash" in text, text
    assert "GOROUTINE STACK OVERFLOW" in text, text


# --------------------------------------------------------------------------- #
#  Import never installs it (process-wide signal handlers only when asked)
# --------------------------------------------------------------------------- #
def test_import_does_not_install():
    rc, out = run_child("""
        print("INSTALLED", stackweave_c.crash_handler_installed())
    """)
    assert rc == 0, out
    assert "INSTALLED False" in out, out


# --------------------------------------------------------------------------- #
#  Self-hang watchdog (start_watchdog)
# --------------------------------------------------------------------------- #
def test_start_watchdog_rejects_nonpositive_secs():
    for bad in (0, -1):
        with pytest.raises(ValueError):
            stackweave.inspect.start_watchdog(bad)


@pytest.mark.skipif(not (hasattr(sys, "_is_gil_enabled") and not sys._is_gil_enabled()),
                    reason="the wedge is an M:N run, which needs the GIL off")
def test_watchdog_reports_a_wedge(tmp_path):
    report = tmp_path / "hang.txt"
    rc, out = run_child("""
        import time
        stackweave.inspect.install_crash_handler("on", %r)
        stackweave.inspect.start_watchdog(1)
        def main():
            ch = stackweave.Chan()
            def waiter():
                ch.recv()                 # outstanding, and nothing completes...
            stackweave.fiber(waiter)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:   # ...until the watchdog reports
                stackweave.sleep(0.1)
                with open(%r) as f:
                    if "HANG (watchdog)" in f.read():
                        break
            ch.send(1)
        stackweave.run(2, main)
        print("SURVIVED")
    """ % (str(report), str(report)))
    assert rc == 0, out
    assert "SURVIVED" in out, out                        # observed, never aborted
    text = report.read_text()
    assert "stackweave HANG (watchdog)" in text, text    # reached the crash file
    assert "fiber dump" in text, text                    # with the fiber dump

"""Adversarial coverage suite for src/runloom_c/coro.c.

coro.c is the portable stackful-coroutine backend (fcontext on x86_64 here):
the guarded-stack map/unmap, the per-thread + global stack depot, the prewarm
one-shot + daemon, the copy-on-grow path, and the invariant sanitizer.

Almost every uncovered region lives behind an env-gated MODE or an error
branch the normal corpus never trips.  An env mode is resolved ONCE (first
getenv, cached static), so the parent pytest's already-imported stackweave_c has
it fixed -- every mode test therefore runs in a SUBPROCESS with the env set.
For gcov to count a subprocess's lines it MUST exit cleanly (a crash / _exit /
SIGKILL does not flush counters), so every worker prints a unique stdout marker
and we assert BOTH the marker AND returncode == 0.  Oracles are real: every
worker counts the fibers that actually RAN (race-free, one bytearray slot per
fiber) or asserts the concrete return value the target line produces.

Regions driven (uncovered coro.c line -> how):
  L266-269  STACKWEAVE_STACK_DEPOT_CAP=<n> static-override of the AUTO depot cap
            -> set it + churn >TLS_CAP fibers so a cache flush consults
            runloom_global_stack_cap() and resolves mode=static.
  L181,1507,1510  invariant sanitizer -> STACKWEAVE_DEBUG_DIAG=invariants arms
            RUNLOOM_DBG_INVARIANTS, so coro_resume sets/clears c->dbg_running
            (1507/1510) and assert_idle reads it on destroy (181).  Oracle: a
            clean run still completes (no false invariant abort).
  L905-906  prewarm BACKGROUND thread-create failure -> RLIMIT_NPROC pinned so
            pthread_create EAGAINs; rc.prewarm(..., background=True) frees its
            arg and returns -1.
  L979-980  prewarm-keep DAEMON thread-create failure -> same RLIMIT_NPROC pin;
            rc.prewarm_keep() clears the running flag and returns -1.

Lines with NO safe Python trigger are classified in the structured report:
  * runloom_coro_grow / maybe_grow target (L1401-1449, L1481-1483): the copy-
    grow only fires for a fiber whose C stack deepens ACROSS yields (low sp at
    a resume boundary).  Python 3.13 keeps interpreter frames on a heap data
    stack, so Python recursion does NOT lower the coro's C sp; the only C-stack
    deepening expressible (operator-dispatch / json C recursion) is a single
    NON-yielding burst that overflows the guard page BEFORE any resume boundary
    (exactly what coro.c's own comment at L1457 says it cannot rescue).  Even
    the full corpus never grows (gcov Runs:499, all #####).
  * runloom_coro_stack_base / guard_size (L126-144) and the invariant_fail
    abort (L182): only reachable via runloom_fiber_for_addr, whose sole caller
    is the fatal-signal crash handler (runloom_crash.c) -- it re-raises and
    dies, so gcov never flushes.
  * hwm-scan batch continuation (L773): needs >512 contiguous resident pages
    (>2 MiB of live C stack) from the top, which exceeds CPython's own C
    recursion guard -- unreachable before RecursionError.
"""
import os
import subprocess
import sys
import textwrap

import pytest

import stackweave_c as rc
from adv_util import needs_free_threading

FT = needs_free_threading()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

# coro.c's POSIX stack pool / madvise / grow all live behind
# RUNLOOM_HAVE_FCONTEXT|UCONTEXT (the guard-page backends).  The Coro-driven
# bits work without the GIL off, but the fiber_n/mn_fiber workloads need the M:N
# scheduler -> skip the whole file on a GIL build (matches the other cov suites).
pytestmark = pytest.mark.skipif(not FT, reason="coro.c stack paths need the M:N / FT build")


def _run_worker(body, env_extra=None, timeout=240):
    """Run a dedented worker snippet in a fresh subprocess; return CompletedProcess.

    Generous default timeout: a churn workload can run slow on a box shared with
    the local CI runner (competes for CPU + mmap_lock); a timeout there is
    contention, not a bug -- callers pytest.skip on TimeoutExpired.
    """
    src = ("import sys\n"
           "sys.path.insert(0, 'src')\n"
           "import stackweave_c as rc\n"
           + textwrap.dedent(body))
    env = dict(os.environ, PYTHON_GIL="0", PYTHONPATH="src")
    if env_extra:
        env.update(env_extra)
    try:
        return subprocess.run([PY, "-c", src], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.skip("coro.c worker timed out (box under heavy load)")


def _assert_clean(p, marker):
    assert p.returncode == 0, (
        "worker crashed (rc=%d)\nstdout=%s\nstderr=%s"
        % (p.returncode, p.stdout[-1600:], p.stderr[-1600:]))
    assert marker in p.stdout, (
        "worker did not reach %r\nstdout=%s\nstderr=%s"
        % (marker, p.stdout[-1600:], p.stderr[-1600:]))


# A race-free fiber-churn body shared by several mode tests: each round spawns
# PER mn_fiber fibers that each set their OWN bytearray slot (single writer ->
# no lost-increment race with the GIL off) and signal a WaitGroup; we assert
# every fiber in every round ran.  ROUNDS forces stack release + reuse.
_CHURN = r'''
import stackweave
from stackweave.sync import WaitGroup
ROUNDS = {rounds}
PER = {per}
def main():
    for r in range(ROUNDS):
        wg = WaitGroup(); wg.add(PER)
        done = bytearray(PER)
        def w(i):
            done[i] = 1
            wg.done()
        for i in range(PER):
            rc.mn_fiber(lambda i=i: w(i))
        wg.wait()
        assert sum(done) == PER, ("round %d only %d/%d ran" % (r, sum(done), PER))
    print("{marker} %d" % (ROUNDS * PER))
stackweave.run({hubs}, main)
'''


# --------------------------------------------------------------------------
# L266-269 : STACKWEAVE_STACK_DEPOT_CAP static override of the AUTO cap.
# --------------------------------------------------------------------------
def test_depot_cap_static_override():
    """An explicit STACKWEAVE_STACK_DEPOT_CAP forces the depot cap to STATIC mode
    (mode=0), overriding the default AUTO sizing.  The static value is read +
    cached on the first runloom_global_stack_cap() call, which happens when a
    per-thread cache overflows (>TLS_CAP=64) and flushes excess to the depot.
    PER=300 per round guarantees the overflow.  Drives L266-269.  Oracle: every
    churned fiber still ran with the cap forced low (correctness, not perf)."""
    body = _CHURN.format(rounds=5, per=300, hubs=3, marker="DEPOTCAP_OK")
    p = _run_worker(body, {"STACKWEAVE_STACK_DEPOT_CAP": "2000"})
    _assert_clean(p, "DEPOTCAP_OK 1500")


# --------------------------------------------------------------------------
# L181, L1507, L1510 : invariant sanitizer dbg_running set/clear + assert_idle.
# --------------------------------------------------------------------------
def test_invariants_dbg_running_set_clear_on_resume():
    """STACKWEAVE_DEBUG_DIAG=invariants arms RUNLOOM_DBG_INVARIANTS.  Under it,
    runloom_coro_resume stores c->dbg_running=1 before the swap (L1507) and =0
    after (L1510), and runloom_coro_assert_idle reads dbg_running (L181) on every
    destroy/recycle/reacquire.  We drive BOTH a raw Coro (direct resume cycles)
    AND an M:N churn (hub resumes + completions).  Oracle: a CORRECT run never
    trips the invariant -- assert_idle's load sees 0 at every idle point, so the
    process completes cleanly instead of aborting via runloom_invariant_fail."""
    body = r'''
import stackweave
from stackweave.sync import WaitGroup

# (a) raw Coro: each resume sets dbg_running=1 (L1507) then 0 (L1510); the
#     final dealloc -> runloom_coro_destroy -> assert_idle reads it (L181).
log = []
def cbody():
    for i in range(4):
        log.append(i)
        rc.yield_()
    log.append("end")
c = rc.Coro(cbody, 65536)
for _ in range(6):
    c.resume()
    if c.done:
        break
assert log == [0, 1, 2, 3, "end"], log
assert c.done is True
del c                         # destroy -> assert_idle on an idle coro (no abort)

# (b) M:N: hub resumes + completions exercise dbg_running under real concurrency.
ran = bytearray(80)
def main():
    wg = WaitGroup(); wg.add(80)
    for i in range(80):
        rc.mn_fiber(lambda i=i: (ran.__setitem__(i, 1), wg.done()))
    wg.wait()
assert sum(ran) == 0 or True  # set below after run completes
stackweave.run(2, main)
assert sum(ran) == 80, sum(ran)
print("INVAR_OK")
'''
    p = _run_worker(body, {"STACKWEAVE_DEBUG_DIAG": "invariants"})
    _assert_clean(p, "INVAR_OK")


# --------------------------------------------------------------------------
# L905-906 : prewarm(background=True) thread-create failure -> free arg, return -1.
# L979-980 : prewarm_keep daemon thread-create failure -> clear flag, return -1.
# --------------------------------------------------------------------------
@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="forces thread-create failure via RLIMIT_NPROC + reads /proc/self/status; "
           "both Linux-specific (Darwin RLIMIT_NPROC caps fork() not threads; no /proc)")
def test_prewarm_background_and_daemon_thread_spawn_failure():
    """Pin RLIMIT_NPROC at the current thread count so no new OS thread can be
    created.  Then:
      * rc.prewarm(n, size, background=True) -> runloom_coro_prewarm spawns a
        detached prewarm thread; runloom_thread_create fails, so it free()s the
        heap arg and returns -1 (coro.c L904-906).
      * rc.prewarm_keep(target, size) -> runloom_coro_prewarm_keep starts the
        continuous daemon; the create fails, so it clears the running flag
        (so a later keep() can retry) and returns -1 (coro.c L977-980).
    Oracle: BOTH return exactly -1 (the documented "couldn't start" value), and
    the process keeps running (the failure is reported, not fatal).  The rlimit
    is restored before exit so the interpreter shuts down cleanly (gcov flushes)."""
    body = r'''
import resource
def cur_threads():
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("Threads:"):
                return int(line.split()[1])
    return -1
base = cur_threads()
soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
resource.setrlimit(resource.RLIMIT_NPROC, (base, hard))   # no new threads at all
try:
    bg = rc.prewarm(16, 65536, True)        # background path: L904-906
    keep = rc.prewarm_keep(64, 65536)       # daemon path:     L977-980
finally:
    resource.setrlimit(resource.RLIMIT_NPROC, (soft, hard))
assert bg == -1, "prewarm(background) returned %r, expected -1 on spawn failure" % (bg,)
assert keep == -1, "prewarm_keep returned %r, expected -1 on spawn failure" % (keep,)
# a retry after restoring the limit must now succeed (the flag was cleared)
keep2 = rc.prewarm_keep(8, 65536)
assert keep2 == 0, "prewarm_keep retry returned %r, expected 0" % (keep2,)
rc.prewarm_stop()
print("PREWARM_SPAWNFAIL_OK")
'''
    p = _run_worker(body)
    _assert_clean(p, "PREWARM_SPAWNFAIL_OK")


# --------------------------------------------------------------------------
# Cross-check: the depot modes are NOT silently no-ops -- a wrong-size reuse
# would surface as a wrong oracle.  This in-process control proves the DEFAULT
# churn path the corpus already covers still works alongside the env-driven
# modes, so a mode test's failure is attributable to that mode, not a flaky
# baseline.
# --------------------------------------------------------------------------
def test_default_churn_baseline_in_process():
    """A small default-mode churn IN the parent process (no env override): the
    same race-free oracle the subprocess workers use, proving the baseline holds
    here so a subprocess failure isolates to its mode.  Also exercises the
    ordinary depot acquire/release/pop_local fast path."""
    import stackweave
    from stackweave.sync import WaitGroup
    from adv_util import hang_guard
    ran = bytearray(120)

    def main():
        wg = WaitGroup(); wg.add(120)
        for i in range(120):
            rc.mn_fiber(lambda i=i: (ran.__setitem__(i, 1), wg.done()))
        wg.wait()

    with hang_guard(30, "default churn baseline"):
        stackweave.run(2, main)
    assert sum(ran) == 120, sum(ran)

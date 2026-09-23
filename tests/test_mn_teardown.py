"""M:N teardown must not deadlock against a hub thread's startup stop-the-world.

mn_fini() joins the hub threads.  On free-threaded 3.13t a hub thread's startup
PyThreadState_New does a qsbr-slot stop-the-world, which waits for EVERY attached
thread to reach a safe point.  If mn_fini joins such a thread (or blocks on a lock
it holds, e.g. runloom_hub_tstate_lock) while the MAIN thread is still ATTACHED,
the STW can never complete -> deadlock.  It is a startup race, so it fires only
when the workload is instant enough that mn_run() returns before a hub thread
finishes starting -- i.e. exactly the trivial M:N programs below.  Was ~80% hang;
the fix detaches the main thread around the hub join.

The hang has no FV model (it is a CPython-runtime STW/attach interaction, not a
runloom lock-free algorithm); the gate is this stress -- a deadlock trips the
suite timeout, so a green run IS the assertion.
"""
import runloom
import runloom_c


def _trivial_cycle(nhubs):
    box = bytearray(1)

    def runner():
        # Touch a cooperative primitive so the runner is a real (if instant) g.
        mu = runloom_c.Mutex()
        with mu:
            pass
        box[0] = 1

    runloom_c.mn_init(nhubs)
    runloom_c.mn_fiber(runner)
    runloom_c.mn_run()
    runloom_c.mn_fini()
    return box[0]


def test_repeated_trivial_mn_teardown():
    # Many instant cycles across hub counts -- each is a fresh shot at the
    # startup-STW-vs-join race.  At the old hang rate a handful would already
    # deadlock; the whole loop completing means teardown is clean.
    for i in range(60):
        nhubs = (i % 4) + 1            # 1, 2, 4(=3+1)... spread small + large
        if nhubs == 3:
            nhubs = 8
        assert _trivial_cycle(nhubs) == 1, i


def test_trivial_mn_teardown_via_run():
    # The public wrapper (runloom.run) takes the same mn_init/mn_run/mn_fini path.
    for i in range(30):
        box = bytearray(1)

        def main():
            box[0] = 1

        runloom.run((i % 4) + 1, main)
        assert box[0] == 1, i


# mn_fini stops the sysmon watchdog first.  The watchdog used to wait out each
# tick in a bare sleep, so the join waited for the rest of it -- and by the end
# of a run the hubs are idle, the idle backoff has stretched the tick to its cap
# (wedge_ns/2, so 25 ms by default), and every runloom.run() paid ~20 ms of pure
# teardown latency.  With RUNLOOM_SYSMON_MS=2000 the backed-off tick is 80 ms,
# which makes the old stall unmistakable next to a prompt join (~1 ms).  The
# watchdog's ticks are phase-locked to mn_init, so a fixed idle time lands at a
# fixed point in the tick: sweep the idle time across one full tick and average,
# else the unfixed stall can hide (a fixed 0.3 s idle measured ~7 ms, not ~40).
_FINI_SNIPPET = r"""
import statistics, time
import runloom, runloom_c

def one_cycle(idle_s):
    runloom_c.mn_init(4)
    states = []
    def main():
        states.extend(runloom_c.mn_hub_states())
        runloom.sleep(idle_s)          # idle long enough for the backoff to cap
    runloom_c.mn_fiber(main)
    runloom_c.mn_run()
    t0 = time.perf_counter()
    runloom_c.mn_fini()
    dt = time.perf_counter() - t0
    assert states and all(s["instrumented"] for s in states), states
    return dt

# 0.30 .. 0.38 s in 10 ms steps: one full 80 ms backed-off tick.
print("FINI_MS", statistics.mean(one_cycle(0.30 + 0.01 * k) for k in range(9)) * 1e3)
"""


def test_fini_does_not_wait_out_the_sysmon_tick():
    import os
    import re
    import subprocess
    import sys
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    env.setdefault("PYTHON_GIL", "0")
    env["RUNLOOM_SYSMON"] = "1"         # watchdog on even where preempt is off
    env["RUNLOOM_SYSMON_QUIET"] = "1"
    env["RUNLOOM_SYSMON_MS"] = "2000"   # backed-off tick = 80 ms
    p = subprocess.run([sys.executable, "-c", _FINI_SNIPPET], env=env,
                       capture_output=True, text=True, timeout=120)
    out = p.stdout + p.stderr
    assert p.returncode == 0, out
    fini_ms = float(re.search(r"FINI_MS (\S+)", out).group(1))
    # Old behaviour: mean ~40 ms (half the 80 ms tick).  Generous bound so a
    # loaded CI box does not flake; still far below the unfixed stall.
    assert fini_ms < 20.0, "mn_fini took %.1f ms -- waiting out the sysmon tick?\n%s" % (fini_ms, out)

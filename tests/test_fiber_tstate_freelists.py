"""A finished fiber's per-g PyThreadState must not leak its object freelists.

Free-threaded CPython caches freed ints, floats, tuples, lists, dicts, ... on
per-thread-state freelists, and ``PyThreadState_Clear`` drains the CURRENT
thread state's freelists, not those of the state it is clearing (CPython's own
threads always clear themselves).  Every M:N fiber has its own thread state,
cleared by whichever thread drops the last reference to the fiber -- so,
before ``runloom_iframe_hand_over_freelists``, whatever a fiber had cached
leaked with it.

The leaked blocks stay "used" in the hub heap they were allocated from
(alloc-home).  When that hub's thread state goes away at ``mn_fini`` its heap
segments are abandoned to the interpreter, but segments holding leaked blocks
can never be reclaimed, so the abandoned pool only grows.  And every later
fiber teardown (``PyThreadState_Clear`` -> mimalloc abandon -> abandoned-pool
sweep) walks that pool: after ~75k ``stackweave.fiber`` children a single
teardown cost close to a millisecond, so a hub fed one fiber per 300 us fell
behind for good -- spawn->first-run latency onto an idle hub went from ~250 us
to tens of milliseconds, for the rest of the process.

The grow-down sampler made it easy to hit: every sampled spawn (and a fresh
closure or lambda is sampled on every spawn) computes a few non-small ints on
the way out, about seven leaked blocks per fiber.

Each scenario runs in a fresh subprocess: the damage is process-wide and
permanent, so it must not leak into (or out of) the rest of the suite.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCENARIO = r'''
import gc, sys, time
sys.path.insert(0, %(src)r)
import stackweave, stackweave_c

HUBS = 4


def remote_spawn_p50_us(n=200, gap=0.0003):
    """p50 of spawn->first-run for mn_fiber(hub=1) from a fiber on hub 0,
    hub 1 otherwise idle (bench.mnsched 'spawn->run remote idle hub')."""
    lat = []
    stackweave_c.mn_init(HUBS)
    try:
        def spawner():
            for _ in range(n):
                t0 = time.perf_counter_ns()

                def f(t0=t0):
                    lat.append(time.perf_counter_ns() - t0)
                stackweave_c.mn_fiber(f, hub=1)
                stackweave_c.sched_sleep(gap)
        stackweave_c.mn_fiber(spawner, hub=0)
        stackweave_c.mn_run()
    finally:
        stackweave_c.mn_fini()
    assert len(lat) == n, len(lat)
    lat.sort()
    return lat[n // 2] / 1e3


def forkjoin(rounds=50, width=100):
    """One hub pool: a root fiber runs `rounds` WaitGroup fork-joins of `width`
    stackweave.fiber children.  Each child is a fresh lambda, so grow-down
    samples (wraps and measures) every one of them."""
    stackweave_c.mn_init(HUBS)
    try:
        def root():
            for _ in range(rounds):
                wg = stackweave.WaitGroup()
                wg.add(width)
                for _ in range(width):
                    stackweave.fiber(lambda wg=wg: wg.done())
                wg.wait()
        stackweave_c.mn_fiber(root)
        stackweave_c.mn_run()
    finally:
        stackweave_c.mn_fini()
    return rounds * width


def allocated_blocks():
    gc.collect()
    return sys.getallocatedblocks()   # includes the abandoned-segment pool


assert stackweave.grow_down_enabled()
before = remote_spawn_p50_us()
forkjoin()                            # warm: imports, TLBC copies, caches
b0 = allocated_blocks()
fibers = 0
for _ in range(15):
    fibers += forkjoin()
b1 = allocated_blocks()
after = remote_spawn_p50_us()
print("RESULT before_us=%%.1f after_us=%%.1f leaked_per_fiber=%%.3f fibers=%%d"
      %% (before, after, (b1 - b0) / fibers, fibers), flush=True)
'''


def _run_scenario():
    env = dict(os.environ, PYTHON_GIL="0")
    p = subprocess.run([sys.executable, "-c", SCENARIO % {"src": os.path.join(REPO, "src")}],
                       cwd=REPO, env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, (p.returncode, p.stdout[-2000:], p.stderr[-4000:])
    line = [ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")]
    assert line, p.stdout[-2000:]
    return {k: float(v) for k, v in (kv.split("=") for kv in line[-1].split()[1:])}


def test_fiber_heavy_load_leaks_nothing_and_keeps_remote_spawn_fast():
    r = _run_scenario()
    # The leak itself: ~7 blocks per fiber before the fix, ~0 after.  Whatever
    # the fibers created is gone, so anything left is per-fiber leakage.
    assert r["leaked_per_fiber"] < 1.0, (
        "finished fibers leaked %.2f allocator blocks each (%r): a per-g "
        "tstate's freelists were not freed with it" % (r["leaked_per_fiber"], r))
    # Its consequence: before the fix the same process measured ~250 us before
    # the load and 11-17 ms after it.  Generous on purpose -- the bug is a
    # 40x+ regression that grows with the load, not a few percent.
    assert r["after_us"] <= max(10 * r["before_us"], 3000.0), (
        "remote spawn->first-run went from %.0f us to %.0f us after %d "
        "stackweave.fiber children (%r)" % (r["before_us"], r["after_us"],
                                            r["fibers"], r))

"""M:N scheduler benchmarks under cross-hub migration (the only M:N mode).

bench.micro times the single-thread scheduler and bench.mn times CPU-bound
core scaling; neither parks a fiber on an M:N hub, so neither sees the paths
that migration-only (#23), Go-style local wake (#24) and the gap fixes (#26)
changed.  This suite times exactly those, on a persistent hub pool created
untimed in each bench's setup:

  park/wake routing   one ping-pong pair, four ways: unpinned (a hub-thread
                      wake lands on the waker's own deque -- local wake),
                      both pinned to one hub, pinned to two different hubs
                      (a pinned g always goes through the global run-queue and
                      a kick), and pinned across two hubs that are each kept
                      busy by a sched_yield loop (the global queue is then
                      reached only on the starve_bound forced turn).
  throughput          spawn, yield, 64 independent pairs, fan-out/fan-in over
                      a buffered channel, a contended Mutex, WaitGroup
                      fork-join, 2-way select, and a blocking call through the
                      blocking() offload pool.
  scaling             the 64-pair ping-pong at 1..N hubs.
  latency             one-way wake latency for a foreign OS thread -> fiber,
                      a fiber -> a fiber pinned to another idle hub,
                      spawn-to-first-run (own hub, remote idle hub,
                      round-robin) and timer lateness for 1 ms sleeps.
  after a load        spawn->run onto a remote idle hub after a
                      stackweave.fiber fork-join load: the grow-down stack
                      sizer leaves remote spawns ~100x slower for the rest of
                      the process (not with optimize("throughput"), which
                      skips grow-down sampling).

Every entry runs in its own fresh interpreter (``--one NAME`` is the child
side): the effect above outlives mn_fini, so in one process a bench's number
could depend on which benches ran before it.

On macOS nothing pins the hubs: under load the scheduler can put hub threads
on efficiency cores, which alone costs ping-pong ~2.4x on an M5 (and ~4.5x
under `taskpolicy -b`).  Result files record the load average and the P/E
core layout; compare runs taken on a quiet machine.

Every bench checks that its fibers did the work (mn_run's completion count,
or a counter) so a silently failing fiber cannot pass as a fast one.

Run:
    PYTHONPATH=src:benchmark PYTHON_GIL=0 python -m bench.mnsched
    ... --quick              # 3 samples, smaller inner counts (smoke)
    ... --only drifted,spawn # just the entries whose names contain these
    ... --in-process         # no per-entry interpreter (faster, order-dependent)
    ... --out results/x.json # write somewhere other than results/mnsched.json

Tunables: STACKWEAVE_BENCH_HUBS (default 4).  Feature switches
(STACKWEAVE_STACK_ARENA, ...) are read by the runtime from the environment and
recorded in the result's env block; bench.features runs the A/B matrix.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

import stackweave
import stackweave_c

from bench.gil import ensure_nogil
from bench.harness import Suite, default_pin_set

HUBS = int(os.environ.get("STACKWEAVE_BENCH_HUBS", "4"))
SCALE_HUBS = [1, 2, 4, 8]


class Pool:
    """setup/teardown pair for Suite.bench: an M:N hub pool that lives for
    every sample of one bench, so neither hub start-up nor mn_fini's join is
    timed."""

    def __init__(self, hubs, drift=False):
        self.hubs = hubs
        self.drift = drift

    def setup(self):
        mn_start(self.hubs)
        if self.drift:
            drift_pending()

    def teardown(self):
        stackweave_c.mn_fini()


# mn_run() returns the completions since mn_init, not since the last mn_run.
_completed = [0]


def mn_start(hubs):
    stackweave_c.mn_init(hubs)
    _completed[0] = 0


def drift_pending(n=16):
    """Leave hub 1 the way unpinned workloads leave hubs at random.  Under
    migration a spawn counts +1 on its placement hub and a completion -1 on
    whichever hub finished it, so per-hub `pending` drifts apart (only the SUM
    is kept exact).  An idle hub whose own pending is <= 0 takes the bare,
    uninterruptible nap instead of the signalled idle-condvar wait, so a
    submit or pinned wake aimed at it sits out that nap.

    Deterministic, so every run measures the drifted case: n fibers are
    spawned on hub 0, re-pin themselves to hub 1 and park; the wake honours
    the pin, so they finish on hub 1 (hub 0 +k, hub 1 -k).  Untimed; prints
    the per-hub pending vector it produced."""
    ch = stackweave_c.Chan(0)

    def mover():
        stackweave_c.current_g().pin(1)
        ch.recv()

    def feeder():
        # Let every mover reach its recv first: a mover that finds a sender
        # already waiting takes the value without parking and stays on hub 0.
        stackweave_c.sched_sleep(0.01)
        for _ in range(n):
            ch.send(1)

    for _ in range(n):
        stackweave_c.mn_fiber(mover, hub=0)
    stackweave_c.mn_fiber(feeder, hub=2)
    run_expect(n + 1)
    print("    (drifted per-hub pending: %s)"
          % [h["pending"] for h in stackweave_c.mn_hub_states()])


def run_expect(n_fibers):
    """mn_run() and insist every fiber of this sample completed."""
    total = stackweave_c.mn_run()
    done, _completed[0] = total - _completed[0], total
    if done != n_fibers:
        raise RuntimeError("mn_run completed %d fibers, expected %d"
                           % (done, n_fibers))


# --------------------------------------------------------------------
# park/wake routing
# --------------------------------------------------------------------
def make_pingpong(n, hub_a=-1, hub_b=-1, busy_hubs=()):
    """One unbuffered ping-pong pair.  inner = n round-trips = 2n wakes.

    busy_hubs get a fiber that loops on sched_yield until the pair is done,
    so those hubs never idle: the pair's wakes must be picked up between
    yields instead of by an idle hub woken by a kick."""
    fiber = stackweave_c.mn_fiber
    sched_yield = stackweave_c.sched_yield

    def once():
        a, b = stackweave_c.Chan(0), stackweave_c.Chan(0)
        stop = [False]
        got = [0]

        def pinger():
            for i in range(n):
                a.send(i)
                b.recv()
            stop[0] = True

        def ponger():
            for _ in range(n):
                v, _ = a.recv()
                b.send(v)
                got[0] += 1

        def spinner():
            while not stop[0]:
                sched_yield()

        for h in busy_hubs:
            fiber(spinner, hub=h)
        fiber(pinger, hub=hub_a)
        fiber(ponger, hub=hub_b)
        run_expect(2 + len(busy_hubs))
        if got[0] != n:
            raise RuntimeError("ping-pong did %d of %d round-trips" % (got[0], n))

    return once


# --------------------------------------------------------------------
# throughput
# --------------------------------------------------------------------
def make_spawn(n):
    """n no-op fibers via the raw mn_fiber path.  inner = n."""
    fiber = stackweave_c.mn_fiber

    def noop():
        pass

    def once():
        for _ in range(n):
            fiber(noop)
        run_expect(n)

    return once


def make_spawn_nested(n):
    """n no-op fibers spawned by a root FIBER through stackweave.fiber -- the
    public path, which optimize("throughput") switches to fiber_fast.
    inner = n."""
    def noop():
        pass

    def once():
        def root():
            f = stackweave.fiber
            for _ in range(n):
                f(noop)
        stackweave_c.mn_fiber(root)
        run_expect(n + 1)

    return once


def make_yield(n_fibers, m):
    """n_fibers each sched_yield m times.  inner = n_fibers * m."""
    fiber = stackweave_c.mn_fiber
    sched_yield = stackweave_c.sched_yield
    count = bytearray(n_fibers)

    def once():
        def mk(k):
            def w():
                for _ in range(m):
                    sched_yield()
                count[k] = 1
            return w
        for k in range(n_fibers):
            count[k] = 0
            fiber(mk(k))
        run_expect(n_fibers)
        if sum(count) != n_fibers:
            raise RuntimeError("only %d of %d yielders finished"
                               % (sum(count), n_fibers))

    return once


def make_pairs(pairs, n):
    """`pairs` independent unbuffered ping-pong pairs, unpinned.
    inner = pairs * n round-trips."""
    fiber = stackweave_c.mn_fiber
    done = bytearray(pairs)

    def once():
        def mk(k):
            a, b = stackweave_c.Chan(0), stackweave_c.Chan(0)

            def pinger():
                for i in range(n):
                    a.send(i)
                    b.recv()

            def ponger():
                for _ in range(n):
                    v, _ = a.recv()
                    b.send(v)
                done[k] = 1
            return pinger, ponger
        for k in range(pairs):
            done[k] = 0
            p, q = mk(k)
            fiber(p)
            fiber(q)
        run_expect(2 * pairs)
        if sum(done) != pairs:
            raise RuntimeError("only %d of %d pairs finished" % (sum(done), pairs))

    return once


def make_fanout(items, workers, cap):
    """1 producer -> `workers` consumers over a Chan(cap), each consumer sends
    one ack per item to a collector over a second Chan(cap).
    inner = items (each item = 2 channel hand-offs)."""
    fiber = stackweave_c.mn_fiber

    def once():
        work = stackweave_c.Chan(cap)
        acks = stackweave_c.Chan(cap)
        total = [0]

        def producer():
            for i in range(items):
                work.send(i)
            work.close()

        def worker():
            while True:
                v, ok = work.recv()
                if not ok:
                    break
                acks.send(v)

        def collector():
            s = 0
            for _ in range(items):
                v, _ = acks.recv()
                s += v
            total[0] = s

        fiber(producer)
        for _ in range(workers):
            fiber(worker)
        fiber(collector)
        run_expect(workers + 2)
        if total[0] != items * (items - 1) // 2:
            raise RuntimeError("fan-out checksum %d != %d"
                               % (total[0], items * (items - 1) // 2))

    return once


def make_mutex(n_fibers, m):
    """n_fibers each take and release one shared Mutex m times; the counter
    it protects must come out exact.  inner = n_fibers * m."""
    fiber = stackweave_c.mn_fiber

    def once():
        mu = stackweave_c.Mutex()
        cnt = [0]

        def w():
            for _ in range(m):
                mu.lock()
                cnt[0] += 1
                mu.unlock()

        for _ in range(n_fibers):
            fiber(w)
        run_expect(n_fibers)
        if cnt[0] != n_fibers * m:
            raise RuntimeError("mutex counter %d != %d" % (cnt[0], n_fibers * m))

    return once


def make_waitgroup(rounds, width):
    """A root fiber runs `rounds` fork-joins of `width` children each
    (WaitGroup add / done / wait).  inner = rounds * width children."""
    def once():
        hit = bytearray(width)
        ok = [0]

        def root():
            for _ in range(rounds):
                wg = stackweave.WaitGroup()
                wg.add(width)

                def child(k):
                    def f():
                        hit[k] = 1
                        wg.done()
                    return f
                for k in range(width):
                    stackweave.fiber(child(k))
                wg.wait()
                ok[0] += sum(hit)
                for k in range(width):
                    hit[k] = 0

        stackweave_c.mn_fiber(root)
        run_expect(rounds * width + 1)
        if ok[0] != rounds * width:
            raise RuntimeError("waitgroup saw %d of %d children"
                               % (ok[0], rounds * width))

    return once


def make_select(n):
    """Two senders on two unbuffered channels, one receiver selecting over
    both.  inner = n received values."""
    fiber = stackweave_c.mn_fiber
    select = stackweave_c.select

    def once():
        a, b = stackweave_c.Chan(0), stackweave_c.Chan(0)
        half = n // 2
        got = [0]

        def sender(ch):
            def f():
                for i in range(half):
                    ch.send(i)
            return f

        def receiver():
            cases = [("recv", a), ("recv", b)]
            for _ in range(2 * half):
                select(cases)
                got[0] += 1

        fiber(sender(a))
        fiber(sender(b))
        fiber(receiver)
        run_expect(3)
        if got[0] != 2 * half:
            raise RuntimeError("select received %d of %d" % (got[0], 2 * half))

    return once


def _blocking_call():
    # Short, genuinely blocking syscall: the thing offload exists for.
    time.sleep(0.0001)
    return 1


def make_blockpool(n_callers, m):
    """n_callers fibers each run m short blocking calls through
    stackweave_c.blocking(), the thread pool that keeps a blocking call off the
    hubs.  inner = n_callers * m."""
    fiber = stackweave_c.mn_fiber
    blocking = stackweave_c.blocking

    def once():
        # One slot per caller: a shared `+=` loses updates with the GIL off.
        per_caller = [0] * n_callers

        def mk(k):
            def caller():
                s = 0
                for _ in range(m):
                    s += blocking(_blocking_call)
                per_caller[k] = s
            return caller

        for k in range(n_callers):
            fiber(mk(k))
        run_expect(n_callers)
        if sum(per_caller) != n_callers * m:
            raise RuntimeError("blocking() returned %d of %d"
                               % (sum(per_caller), n_callers * m))

    return once


# --------------------------------------------------------------------
# latency distributions (one sample per event)
# --------------------------------------------------------------------
def lat_foreign_wake(hubs, n, gap_s=0.0002):
    """A plain OS thread sends a perf_counter_ns stamp to a fiber parked on an
    unbuffered channel.  A foreign waker has no deque, so every wake goes
    through the global run-queue and a hub kick.  Returns one-way ns."""
    lat = []
    mn_start(hubs)
    try:
        ch = stackweave_c.Chan(0)

        def rx():
            for _ in range(n):
                v, _ = ch.recv()
                lat.append(time.perf_counter_ns() - v)

        def tx():
            for _ in range(n):
                while True:
                    try:
                        ch.send(time.perf_counter_ns())
                        break
                    except RuntimeError:
                        pass    # receiver not parked yet: a foreign thread can't block
                time.sleep(gap_s)

        stackweave_c.mn_fiber(rx)
        t = threading.Thread(target=tx)
        t.start()
        run_expect(1)
        t.join()
    finally:
        stackweave_c.mn_fini()
    return lat


def lat_cross_hub_wake(hubs, n, gap_s=0.0002):
    """A fiber pinned to hub 0 sends a stamp to a fiber pinned to hub 1, which
    is otherwise idle.  A pinned g's wake goes to the global run-queue; the
    idle target hub has to be kicked awake to pull it.  Returns one-way ns."""
    lat = []
    mn_start(hubs)
    try:
        ch = stackweave_c.Chan(0)

        def rx():
            for _ in range(n):
                v, _ = ch.recv()
                lat.append(time.perf_counter_ns() - v)

        def tx():
            for _ in range(n):
                ch.send(time.perf_counter_ns())
                stackweave_c.sched_sleep(gap_s)

        stackweave_c.mn_fiber(rx, hub=1)
        stackweave_c.mn_fiber(tx, hub=0)
        run_expect(2)
    finally:
        stackweave_c.mn_fini()
    return lat


def lat_spawn(hubs, target_hub, n, gap_s=0.0003, drift=False):
    """Spawn-to-first-run: a fiber on hub 0 spawns a fiber that records how
    long it took to start, then sleeps so the target hub goes idle again.
    target_hub=-1 is round-robin placement.  Returns ns."""
    lat = []
    mn_start(hubs)
    try:
        if drift:
            drift_pending()

        def spawner():
            for _ in range(n):
                t0 = time.perf_counter_ns()

                def f(t0=t0):
                    lat.append(time.perf_counter_ns() - t0)
                stackweave_c.mn_fiber(f, hub=target_hub)
                stackweave_c.sched_sleep(gap_s)

        stackweave_c.mn_fiber(spawner, hub=0)
        run_expect(n + 1)
    finally:
        stackweave_c.mn_fini()
    return lat


def lat_timer(hubs, n_fibers, rounds, sleep_s=0.001):
    """n_fibers each sleep `sleep_s` `rounds` times; lateness = time actually
    slept - sleep_s.  Returns ns (clamped at 0)."""
    lat = [0] * (n_fibers * rounds)
    mn_start(hubs)
    try:
        def mk(k):
            def f():
                for r in range(rounds):
                    t0 = time.perf_counter_ns()
                    stackweave_c.sched_sleep(sleep_s)
                    lat[k * rounds + r] = max(
                        0, time.perf_counter_ns() - t0 - int(sleep_s * 1e9))
            return f
        for k in range(n_fibers):
            stackweave_c.mn_fiber(mk(k))
        run_expect(n_fibers)
    finally:
        stackweave_c.mn_fini()
    return lat


# --------------------------------------------------------------------
def forkjoin_load(hubs, rounds):
    """`rounds` x 5000 stackweave.fiber WaitGroup children (grow-down on)."""
    p = Pool(hubs)
    p.setup()
    try:
        once = make_waitgroup(50, 100)
        for _ in range(rounds):
            once()
    finally:
        p.teardown()


def plan(H, quick, scale):
    """[(section, name, run(suite))] -- each entry is self-contained (its own
    hub pool(s)), so it can run alone in a fresh process."""
    q = 10 if quick else 1               # inner-count divisor
    n = 400 if quick else 4_000          # latency events
    P = []

    def bench(section, name, mk, inner, note, pool=None, **kw):
        pool = pool or Pool(H)
        P.append((section, name, lambda s: s.bench(
            name, mk(), inner=inner, note=note,
            setup=pool.setup, teardown=pool.teardown, **kw)))

    def lat(section, name, fn, note):
        P.append((section, name, lambda s: s.latency(name, fn(), note=note)))

    bench("park/wake routing", "pingpong local-wake",
          lambda: make_pingpong(100_000 // q), 100_000 // q,
          "unpinned pair: hub-thread wake -> waker's own deque")
    bench("park/wake routing", "pingpong same-hub pinned",
          lambda: make_pingpong(100_000 // q, 0, 0), 100_000 // q,
          "both pinned to hub 0: global run-queue, never leaves the hub")
    bench("park/wake routing", "pingpong cross-hub pinned",
          lambda: make_pingpong(10_000 // q, 0, 1), 10_000 // q,
          "pinned to hubs 0 and 1: global run-queue + kick into an idle hub")
    bench("park/wake routing", "pingpong cross-hub pinned drifted",
          lambda: make_pingpong(2_000 // q, 0, 1), 2_000 // q,
          "as above after per-hub pending has drifted (hub 1 < 0): the "
          "target hub naps uninterruptibly", pool=Pool(H, drift=True))
    bench("park/wake routing", "pingpong cross-hub busy",
          lambda: make_pingpong(1_000 // q, 0, 1, (0, 1)), 1_000 // q,
          "as above, both hubs busy in a sched_yield loop: runq reached only "
          "on the starve_bound forced turn", samples=3 if quick else 6)

    bench("throughput", "spawn noop mn_fiber", lambda: make_spawn(20_000 // q),
          20_000 // q, "raw mn_fiber from the main thread")
    bench("throughput", "spawn noop stackweave.fiber",
          lambda: make_spawn_nested(20_000 // q), 20_000 // q,
          "public spawn path from a root fiber (optimize-sensitive)")
    bench("throughput", "yield 1000 fibers x200", lambda: make_yield(1_000, 200 // q),
          1_000 * (200 // q), "sched_yield on M:N hubs")
    bench("throughput", "64 pairs pingpong", lambda: make_pairs(64, 2_000 // q),
          64 * (2_000 // q), "aggregate park/wake, unpinned")
    bench("throughput", "fan-out 1->32 buf64", lambda: make_fanout(100_000 // q, 32, 64),
          100_000 // q, "producer -> 32 workers -> collector")
    bench("throughput", "mutex 64 fibers contended", lambda: make_mutex(64, 2_000 // q),
          64 * (2_000 // q), "stackweave_c.Mutex lock/unlock")
    wg_rounds = 50 // min(q, 5)
    bench("throughput", "waitgroup fork-join 100x50", lambda: make_waitgroup(wg_rounds, 100),
          wg_rounds * 100, "root fiber: 100 children per round, WaitGroup.wait")
    bench("throughput", "select 2 chans", lambda: make_select(50_000 // q),
          50_000 // q, "1 receiver selecting over 2 unbuffered senders")
    bench("throughput", "blocking() sleep(100us)", lambda: make_blockpool(32, 100 // q),
          32 * (100 // q), "32 callers, blocking-offload thread pool")

    for h in scale:
        bench("scaling: 64 pairs pingpong", "64 pairs pingpong @%dh" % h,
              lambda: make_pairs(64, 2_000 // q), 64 * (2_000 // q),
              "%d hubs" % h, pool=Pool(h))

    lat("latency", "foreign thread -> fiber wake", lambda: lat_foreign_wake(H, n),
        "plain OS thread send; global run-queue + kick")
    lat("latency", "cross-hub pinned wake", lambda: lat_cross_hub_wake(H, n),
        "fiber on hub 0 -> fiber pinned to idle hub 1")
    lat("latency", "spawn->run own hub", lambda: lat_spawn(H, 0, n // 4),
        "mn_fiber(hub=0) from a fiber on hub 0")
    lat("latency", "spawn->run remote idle hub", lambda: lat_spawn(H, 1, n // 4),
        "mn_fiber(hub=1) from hub 0; hub 1 idle, nothing pending")
    lat("latency", "spawn->run round-robin", lambda: lat_spawn(H, -1, n // 4),
        "mn_fiber() placement from a fiber on hub 0")
    lat("latency", "timer lateness 1ms",
        lambda: lat_timer(H, 200 if quick else 1_000, 10),
        "actual sleep - 1 ms, 1000 fibers x 10 sleeps")

    load_rounds = 2 if quick else 10

    def after_forkjoin():
        forkjoin_load(H, load_rounds)
        return lat_spawn(H, 1, n // 4)
    lat("after a load", "spawn->run remote idle hub, after fork-join load",
        after_forkjoin,
        "as 'spawn->run remote idle hub', after %d x 5000 stackweave.fiber "
        "WaitGroup children in this process (grow-down on)" % load_rounds)

    return P


def run_isolated(argv, out_json):
    """Re-run this module for one entry in a fresh interpreter; return its
    result document."""
    cmd = [sys.executable, "-X", "gil=0", "-m", "bench.mnsched"] + argv + ["--out", out_json]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("%s failed (rc=%s):\n%s" % (
            " ".join(cmd[3:]), p.returncode,
            "\n".join((p.stdout + p.stderr).splitlines()[-20:])))
    with open(out_json) as f:
        return json.load(f)


def main(argv=None):
    ensure_nogil()
    ap = argparse.ArgumentParser(description="M:N scheduler benchmarks")
    ap.add_argument("--quick", action="store_true",
                    help="3 samples, smaller inner counts (smoke run)")
    ap.add_argument("--out", default=None, help="result JSON path")
    ap.add_argument("--hubs", type=int, default=HUBS)
    ap.add_argument("--no-scaling", action="store_true")
    ap.add_argument("--no-latency", action="store_true")
    ap.add_argument("--only", default=None,
                    help="comma-separated substrings: run only matching entries")
    ap.add_argument("--one", default=None, help=argparse.SUPPRESS)  # child side
    ap.add_argument("--in-process", action="store_true",
                    help="run every entry in this process (faster, but later "
                         "numbers then depend on what ran before)")
    args = ap.parse_args(argv)

    H = args.hubs
    ncpu = os.cpu_count() or H
    scale = [] if args.no_scaling else [h for h in SCALE_HUBS if h <= ncpu]
    entries = plan(H, args.quick, scale)
    if args.no_latency:
        entries = [e for e in entries if e[0] not in ("latency", "after a load")]
    if args.only:
        pats = [x for x in args.only.split(",") if x]
        entries = [e for e in entries if any(x in e[1] for x in pats)]
    s = Suite("mnsched", pin_cpus=default_pin_set(n=max(scale + [H + 2])),
              samples=3 if args.quick else 12, warmup=1 if args.quick else 3)

    if args.one is not None:                       # child: exactly one entry
        for _, name, run in entries:
            if name == args.one:
                run(s)
                s.write(args.out)
                return
        raise SystemExit("no such entry: %r" % args.one)

    s.banner()
    print("hubs: %d (scaling: %s), %s\n" % (
        H, scale or "off", "one process" if args.in_process
        else "one fresh interpreter per entry"))
    child_argv = ["--hubs", str(H)] + (["--quick"] if args.quick else [])
    if args.no_scaling:
        child_argv.append("--no-scaling")
    section = None
    with tempfile.TemporaryDirectory() as tmp:
        for i, (sec, name, run) in enumerate(entries):
            if sec != section:
                print("%s%s" % ("\n" if section else "", sec))
                section = sec
            if args.in_process:
                run(s)
                continue
            doc = run_isolated(child_argv + ["--one", name],
                               os.path.join(tmp, "%d.json" % i))
            for r in doc.get("results", []):
                s.results.append(r)
                s.print_row(r)
            for r in doc.get("latency", []):
                s.latency_results.append(r)
                print("  %-34s p50=%8.1fus  p90=%8.1fus  p99=%8.1fus  max=%9.1fus  n=%d"
                      % (r["name"], r["p50_us"], r["p90_us"], r["p99_us"],
                         r["max_us"], r["count"]))
    sc = {r["name"]: r for r in s.results if r["name"].startswith("64 pairs pingpong @")}
    if sc:
        rows = sorted(sc.values(), key=lambda r: int(r["name"].split("@")[1][:-1]))
        base = rows[0]["ops_per_s"]
        print("\n  %-8s %14s %9s" % ("hubs", "round-trips/s", "vs %s" % rows[0]["name"].split("@")[1]))
        for r in rows:
            print("  %-8s %14.0f %8.2fx" % (r["name"].split("@")[1], r["ops_per_s"],
                                            r["ops_per_s"] / base))
    s.write(args.out)


if __name__ == "__main__":
    main()

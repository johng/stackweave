"""Memory per parked fiber on stackweave (see bench/appspec.py).

Spawns N fibers through the public stackweave.fiber path, each parked on one
unbuffered channel, and records the process's RSS growth per fiber once all
of them are parked (the OS's number, not an allocator estimate).  Run under
each feature config by bench.compare: grow-down stacks, STACK_ARENA and
optimize("throughput") all change it.  bench.baselines and gobench/ park the
same number of threads / tasks / greenlets / goroutines.

Run:
    PYTHONPATH=src:benchmark PYTHON_GIL=0 python -m bench.memory [--quick] [--out x.json]
"""
import argparse
import gc
import os

import stackweave
import stackweave_c

from bench import appspec as A
from bench.gil import ensure_nogil
from bench.harness import Suite

HUBS = int(os.environ.get("STACKWEAVE_BENCH_HUBS", "4"))


def park(s, n):
    ch = stackweave_c.Chan(0)
    ready = bytearray(n)
    wg = stackweave.WaitGroup()
    wg.add(n)

    def unit(k):
        def f():
            ready[k] = 1
            ch.recv()
            wg.done()
        return f

    gc.collect()
    before = A.rss_bytes()
    for k in range(n):
        stackweave.fiber(unit(k))
    while sum(ready) < n:
        stackweave.sleep(0.01)
    stackweave.sleep(0.2)                   # let the last ones finish parking
    after = A.rss_bytes()
    s.memory(A.mem_row(n), n, before, after, note="stackweave.fiber parked on Chan(0).recv")
    ch.close()
    wg.wait()


def main(argv=None):
    ensure_nogil()
    ap = argparse.ArgumentParser(description="RSS per parked fiber")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    s = Suite("memory", pin_cpus=[])
    s.banner()
    counts = [n // 10 for n in A.MEM_COUNTS] if args.quick else list(A.MEM_COUNTS)
    errors = []

    def root():
        try:
            for n in counts:
                park(s, n)
        except BaseException as e:          # noqa: BLE001 -- re-raised after run()
            errors.append(e)

    stackweave.run(HUBS, root)
    if errors:
        raise errors[0]
    s.write(args.out)


if __name__ == "__main__":
    main()

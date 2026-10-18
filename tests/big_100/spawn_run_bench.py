"""1M-fiber spawn + RUN to completion on 8 hubs: time and correctness.

fiber_n(noop, N) then mn_run() (drains all N on the hubs).  mn_run returns the
completed count, so == N proves every fiber ran.  SPAWN_N sets N.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import stackweave_c

N = int(os.environ.get("SPAWN_N", "1000000"))


def noop():
    pass


def main():
    stackweave_c.mn_init(8)
    print("N={0} hubs=8  (spawn + run to completion)".format(N))

    t0 = time.monotonic()
    stackweave_c.fiber_n(noop, N)
    t_spawn = time.monotonic() - t0

    t1 = time.monotonic()
    done = stackweave_c.mn_run()
    t_run = time.monotonic() - t1

    total = time.monotonic() - t0
    ok = "OK" if done == N else "!!! MISMATCH"
    print("  spawn : {0:.3f}s".format(t_spawn))
    print("  run   : {0:.3f}s".format(t_run))
    print("  TOTAL : {0:.3f}s   ({1:.0f}k g/s)".format(total, N / total / 1000))
    print("  completed: {0}/{1}  {2}".format(done, N, ok))
    stackweave_c.mn_fini()


if __name__ == "__main__":
    main()

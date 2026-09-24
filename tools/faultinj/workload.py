"""Compact stackweave workload for fault-injection sweeps.

Exercises the paths whose cleanup branches the coverage report flagged as
untested: channel send/recv, the single-thread scheduler, the M:N scheduler
(hub threads -> eventfd/epoll), and a timed park (timerfd / deadline heap).
Prints WORKLOAD_OK on success; any crash/hang on an injected failure is a
cleanup-path bug.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import stackweave
import stackweave_c


def producer_consumer():
    ch = stackweave_c.Chan(4)

    def prod():
        for i in range(8):
            ch.send(i)
        ch.close()

    def cons():
        total = 0
        while True:
            v, ok = ch.recv()
            if not ok:
                break
            total += v
        return total

    stackweave_c.fiber(prod)
    stackweave_c.fiber(cons)
    stackweave_c.run()


def mn_round():
    stackweave_c.mn_init(2)
    for i in range(16):
        stackweave_c.mn_fiber(lambda n=i: n * 2)
    stackweave_c.mn_run()
    stackweave_c.mn_fini()


def timed_park():
    def g():
        stackweave.sleep(0.001)
    # stackweave.run is the high-level entry: run(n, main_fn) on n hubs; the sleep
    # parks on the deadline heap (epoll_wait timeout) and is woken on expiry.
    # Was `stackweave.fiber(g); stackweave.run()`, but run() requires the hub-count n,
    # so it raised TypeError and aborted the workload before this timed-park stage
    # (and the self_check below) ran at all under injection.
    stackweave.run(1, g)


def main():
    producer_consumer()
    mn_round()
    timed_park()
    assert stackweave_c._self_check(0) == 0, "self_check failed after injected fault"
    print("WORKLOAD_OK")


if __name__ == "__main__":
    main()

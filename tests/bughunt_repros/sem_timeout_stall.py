"""Weighted Semaphore: a timed-out FRONT waiter is removed but the waiters
behind it are never re-scanned for grants -> a fitting waiter stalls forever
even though permits are free.  (Go's x/sync semaphore notifies other waiters
when a front waiter's ctx is cancelled.)"""
import sys
import stackweave
from stackweave.sync import Semaphore

state = {"b_acquired": False, "a_timed_out": False}

def main():
    sem = Semaphore(2)
    sem.acquire(1)          # long-lived holder: held=1

    def a():
        # wants 2 permits: held+2 > 2 -> queues at the FRONT
        ok = sem.acquire(2, timeout=0.3)
        state["a_timed_out"] = not ok

    def b():
        # wants 1 permit: it FITS (held+1 <= 2) but FIFO queues it behind A
        sem.acquire(1)
        state["b_acquired"] = True

    stackweave.fiber(a)
    stackweave.sleep(0.05)     # let A queue first
    stackweave.fiber(b)
    stackweave.sleep(1.0)      # A times out at t=0.3; B should be granted then
    print("a_timed_out =", state["a_timed_out"])
    print("b_acquired  =", state["b_acquired"], "(expected True)")
    if not state["b_acquired"]:
        print("BUG: B stalled even though a permit is free")
        # unblock so run() can exit
        sem.release(1)
        stackweave.sleep(0.1)
        sys.exit(1)

stackweave.run(1, main)

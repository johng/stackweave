# Repro: single-thread sched_yield fast path ignores the TIMER heap.
# Fiber A parks with a timeout (stackweave_c.park(timeout=0.05) -- the primitive
# stackweave.sync Lock/Event timeouts build on).  Fiber B poll-loops on
# stackweave_c.sched_yield().  The yield fast path (runloom_sched_parkwake.c.inc:93)
# checks ready/sleep/netpoll/blockpool but NOT s->timer_size, so it never
# returns to the drain loop and A's timeout never fires -> hang.
import stackweave_c, time, sys

state = {"woke": False, "b_iters": 0}

def A():
    r = stackweave_c.park(timeout=0.05)   # should time out after 50ms
    state["woke"] = True
    print("A resumed, timed_out =", r, "after", time.monotonic() - t0, "s")

def B():
    # cooperative poll loop: yields every iteration -- SHOULD let the
    # scheduler fire A's 50ms timer.
    while not state["woke"]:
        state["b_iters"] += 1
        stackweave_c.sched_yield()
        if time.monotonic() - t0 > 3.0:
            print("BUG: A's 50ms park timeout never fired after 3s of B yielding",
                  "(b_iters=%d)" % state["b_iters"])
            sys.exit(1)

t0 = time.monotonic()
stackweave_c.fiber(A)
stackweave_c.fiber(B)
stackweave_c.run()
print("OK: total", time.monotonic() - t0)

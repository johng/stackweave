import stackweave_c, time, sys
from stackweave import sync
ev = sync.Event()
state = {"done": False}
def A():
    r = ev.wait(timeout=0.05)
    state["done"] = True
    print("Event.wait returned", r, "after", round(time.monotonic() - t0, 4), "s")
def B():
    while not state["done"]:
        stackweave_c.sched_yield()
        if time.monotonic() - t0 > 3.0:
            print("BUG: Event.wait(timeout=0.05) starved for 3s by yielding fiber")
            sys.exit(1)
t0 = time.monotonic()
stackweave_c.fiber(A)
stackweave_c.fiber(B)
stackweave_c.run()

# Evidence for a biased-refcount merge on the wrong OS thread.
# Objects are allocated by fiber P (unpinned) and dropped by consumer C on
# another hub, so each drop queues the object to the ALLOCATING hub's bucket;
# only that hub's thread may merge it (_Py_ExplicitMergeRefcount writes
# ob_ref_local non-atomically).  A-objects' __del__ parks and migrates; if that
# __del__ ran from a fiber's own eval-breaker merge with more objects left in
# its local_objects_to_merge stack, runloom_iframe_brc_release (which only
# checks the shared queue) does not drain them, and the fiber merges the rest
# after resuming on ANOTHER hub.  B-objects record the thread their __del__ ran
# on; with gc disabled, a B destroyed off its allocating thread is that bug.
from prelude import *
import gc
gc.disable()
H = int(sys.argv[1]) if len(sys.argv) > 1 else 4
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 400
SPIN = int(sys.argv[3]) if len(sys.argv) > 3 else 20000
_watchdog(110)
stats = {"a_in_fiber": 0, "a_del": 0, "a_moved": 0, "b_del": 0, "b_off_owner": 0, "b_off_owner_nofiber": 0}
class A:
    __slots__ = ("owner",)
    def __init__(self): self.owner = threading.get_ident()
    def __del__(self):
        t0 = threading.get_ident()
        if stackweave_c.current_g() is not None:
            stats["a_in_fiber"] += 1
            force_migrate(rounds=3, pin_fallback=True)
        stats["a_del"] += 1
        if threading.get_ident() != t0: stats["a_moved"] += 1
class B:
    __slots__ = ("owner",)
    def __init__(self): self.owner = threading.get_ident()
    def __del__(self):
        stats["b_del"] += 1
        if threading.get_ident() != self.owner:
            stats["b_off_owner"] += 1
            if stackweave_c.current_g() is None: stats["b_off_owner_nofiber"] += 1
def main():
    ch = stackweave.Chan(0)
    wg = stackweave.WaitGroup(); wg.add(2)
    def producer():
        for r in range(ROUNDS):
            ch.send([B(), B(), B(), A()])     # C drops B,B,B then A: LIFO pops A first
            for _ in range(SPIN): pass        # stay busy so the eval breaker drains here
        ch.send(None); wg.done()
    def consumer():
        while True:
            v, ok = ch.recv()
            if v is None: break
            while v: v.pop(0)               # drop B, B, B, then A
        wg.done()
    stackweave_c.mn_fiber(producer, hub=0)
    stackweave_c.mn_fiber(consumer, hub=1)
    wg.wait()
    stackweave.sleep(0.3)
    print(stats, flush=True)
    st = stackweave_c.stats(); print({k: v for k, v in st.items() if k.startswith("brc")}, flush=True)
    print("PASS" if stats["b_off_owner"] == 0 else "FAIL: %d objects merged+freed off their owner thread" % stats["b_off_owner"], flush=True)
stackweave.run(H, main)

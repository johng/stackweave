# Objects whose __del__ parks (stackweave.sleep) are allocated on one hub and
# dropped (last ref) on another, so the dealloc runs from a biased-refcount
# merge drain -- on a fiber's eval breaker, or on the hub stack in
# runloom_iframe_brc_release.  What happens to the park?
from prelude import *
import gc
mode = sys.argv[1] if len(sys.argv) > 1 else "sleep"
H = int(sys.argv[2]) if len(sys.argv) > 2 else 4
N = int(sys.argv[3]) if len(sys.argv) > 3 else 2000
_watchdog(40)
stats = {"del": 0, "del_err": 0, "errs": set(), "del_tids": set(), "moved_in_del": 0}
unraisable = []
sys.unraisablehook = lambda u: unraisable.append(repr(u.exc_value)[:120])
class Obj:
    __slots__ = ("payload", "__weakref__")
    def __init__(self): self.payload = [1, 2, 3]
    def __del__(self):
        t0 = threading.get_ident()
        g = stackweave_c.current_g()
        ctx = "fiber" if g is not None else "no-fiber"
        stats.setdefault("ctx", {}); stats["ctx"][ctx] = stats["ctx"].get(ctx, 0) + 1
        if g is None:
            import traceback
            stack = "".join(traceback.format_stack(limit=3)).replace("\n", " | ")
            stats.setdefault("nofiber_stacks", set()).add(stack[-200:])
        tt = __import__("time").perf_counter()
        try:
            if mode == "sleep":
                stackweave.sleep(0.0005)
            elif mode == "chan":
                c = stackweave.Chan(0)
                stackweave.fiber(lambda: c.send(1))
                c.recv()
            stats["del"] += 1
        except BaseException as e:
            stats["del_err"] += 1; stats["errs"].add(repr(e)[:120])
        if threading.get_ident() != t0: stats["moved_in_del"] += 1
        if g is None: stats["nofiber_s"] = stats.get("nofiber_s", 0) + (__import__("time").perf_counter() - tt)
def main():
    ch = stackweave.Chan(64)
    wg = stackweave.WaitGroup(); wg.add(2)
    def producer():
        for i in range(N):
            ch.send(Obj())
        ch.send(None); wg.done()
    def consumer():
        held = []
        while True:
            v, ok = ch.recv()
            if v is None: break
            held.append(v)
            if len(held) > 50:
                held.clear()     # drop 51 objects owned by the producer's hub
        held.clear(); wg.done()
    stackweave_c.mn_fiber(producer, hub=0)
    stackweave_c.mn_fiber(consumer, hub=1)
    wg.wait()
    for _ in range(3):
        gc.collect(); stackweave.sleep(0.05)
    print({k: (sorted(v) if isinstance(v, set) else v) for k, v in stats.items()}, "unraisable:", unraisable[:3], len(unraisable), flush=True)
    st = stackweave_c.stats()
    print({k: v for k, v in st.items() if "brc" in k}, flush=True)
    print("PASS" if stats["del"] == N and not unraisable else "FAIL", flush=True)
stackweave.run(H, main)

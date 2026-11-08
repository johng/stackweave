# Fiber A holds a lock across CPU-bound Python (gets preempted, stays on hub 0);
# fiber B on hub 0 then contends.  Threads: B waits ~0.3 s.  Fibers?
from prelude import *
import time as _t
kind = sys.argv[1]           # stock | patched | rlock | strptime
if kind in ("patched", "strptime-patched"):
    import stackweave.monkey as monkey; monkey.patch()
_watchdog(15)
def step(end): return _t.perf_counter() < end
def main():
    out = {}
    if kind in ("stock", "patched"): lock = threading.Lock()
    elif kind == "rlock": lock = threading.RLock()
    wg = stackweave.WaitGroup(); wg.add(2)
    def A():
        if kind.startswith("strptime"):
            import _strptime
            with _strptime._cache_lock:
                end = _t.perf_counter() + 0.3
                while step(end): pass
        else:
            with lock:
                end = _t.perf_counter() + 0.3
                while step(end): pass
        out["A"] = "done"; wg.done()
    def B():
        t0 = _t.perf_counter()
        if kind.startswith("strptime"):
            _t.strptime("2026-11-08", "%Y-%m-%d")
        else:
            with lock: pass
        out["B_wait_s"] = round(_t.perf_counter() - t0, 3)
        out["B_hub"] = stackweave_c.mn_current_hub(); wg.done()
    stackweave_c.mn_fiber(A, hub=0)
    stackweave.sleep(0.02)
    stackweave_c.mn_fiber(B, hub=0)
    wg.wait()
    print(kind, out, "PASS", flush=True)
stackweave.run(2, main)

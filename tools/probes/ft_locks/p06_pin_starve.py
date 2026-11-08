# A CPU-bound fiber on hub 0 is preempted every ~50 ms; a fiber that pins itself
# to hub 0 (g.pin(0) + sleep(0)) -- how long until it first runs there?
from prelude import *
import time as _t
def step(end): return _t.perf_counter() < end
def main():
    out = {}
    wg = stackweave.WaitGroup(); wg.add(2)
    def hog():
        stackweave_c.mn_current_hub()
        end = _t.perf_counter() + 6
        while step(end): pass
        wg.done()
    def pinned():
        t0 = _t.perf_counter()
        stackweave_c.current_g().pin(0); stackweave.sleep(0)
        out["first_run_after_s"] = round(_t.perf_counter() - t0, 3)
        out["hub"] = stackweave_c.mn_current_hub()
        wg.done()
    stackweave_c.mn_fiber(hog, hub=0)
    stackweave.sleep(0.2)
    stackweave_c.mn_fiber(pinned, hub=1)
    wg.wait()
    print(out, flush=True)
stackweave.run(2, main)

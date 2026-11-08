from prelude import *
import time as _t
seq = []
def step(end): return _t.perf_counter() < end
def hog(name):
    for _ in range(30):
        end = _t.perf_counter() + 0.01
        while step(end): pass
        if not seq or seq[-1] != name: seq.append(name)
mode = sys.argv[1]
if mode == "mn":
    stackweave_c.mn_init(2)
    stackweave_c.mn_fiber(lambda: hog("A"), hub=0); stackweave_c.mn_fiber(lambda: hog("B"), hub=0)
    stackweave_c.mn_run(); stackweave_c.mn_fini()
elif mode == "mn_pin":
    stackweave_c.mn_init(2)
    for n in "AB":
        stackweave_c.mn_fiber(lambda n=n: (stackweave_c.current_g().pin(0), stackweave.sleep(0), hog(n)))
    stackweave_c.mn_run(); stackweave_c.mn_fini()
elif mode.startswith("run"):
    def main():
        wg = stackweave.WaitGroup(); wg.add(2)
        for n in "AB":
            if mode == "run_hub": stackweave_c.mn_fiber(lambda n=n: (hog(n), wg.done()), hub=0)
            else: stackweave.fiber(lambda n=n: (stackweave_c.current_g().pin(0), stackweave.sleep(0), hog(n), wg.done()))
        wg.wait()
    stackweave.run(2, main)
print(mode, "".join(seq))

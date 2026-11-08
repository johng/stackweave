# The stopit / func_timeout idiom: a watchdog records threading.get_ident() in
# the fiber it wants to interrupt, then calls PyThreadState_SetAsyncExc(ident).
# Which fiber gets the exception?
from prelude import *
import ctypes, time as _t
class Timeout(Exception): pass
def main():
    got = {}
    wg = stackweave.WaitGroup(); wg.add(2)
    def target():
        ident = threading.get_ident()
        def fire():
            _t.sleep(0.1)
            n = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(ident), ctypes.py_object(Timeout))
            got["n_set"] = n
        threading.Thread(target=fire, daemon=True).start()
        try:
            stackweave.sleep(0.3)          # parked when the exception is posted
            for _ in range(200000): pass   # then run some bytecode
            got["target"] = "not interrupted"
        except Timeout:
            got["target"] = "interrupted"
        wg.done()
    def bystander():
        try:
            end = _t.perf_counter() + 0.5
            while _t.perf_counter() < end: [0] * 10
            got["bystander"] = "ok"
        except Timeout:
            got["bystander"] = "GOT THE TIMEOUT"
        wg.done()
    stackweave_c.mn_fiber(target, hub=0)
    stackweave_c.mn_fiber(bystander, hub=0)
    wg.wait()
    print(got, flush=True)
    print("PASS" if got.get("target") == "interrupted" and got.get("bystander") == "ok" else "FAIL", flush=True)
stackweave.run(2, main)

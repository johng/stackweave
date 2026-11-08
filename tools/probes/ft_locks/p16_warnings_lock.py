# The interpreter's warnings lock (_PyRecursiveMutex, OS-thread owner) is held
# by C do_warn across showwarning -> linecache.getline -> os.stat, which
# monkey.patch offloads (the fiber parks and is woken from a pool thread, so
# it may resume on another hub).  Afterwards, can other hubs still warn?
from prelude import *
import stackweave.monkey as monkey; monkey.patch()
import warnings, tempfile, importlib, io
_watchdog(30)
H = int(sys.argv[1]) if len(sys.argv) > 1 else 4
sys.stderr = io.StringIO() if len(sys.argv) > 2 else sys.stderr
d = tempfile.mkdtemp(); sys.path.insert(0, d)
def mkmod(i):
    with open(os.path.join(d, "wmod%d.py" % i), "w") as f:
        f.write("import warnings\ndef go():\n    warnings.warn('first %d', UserWarning)\n" % i)
    return importlib.import_module("wmod%d" % i)
MODE = os.environ.get("WMODE", "stat")
if MODE == "showwarning":
    _orig_show = warnings.showwarning
    def _show(*a, **k):
        # a handler that does I/O (e.g. logging.captureWarnings -> socket handler)
        if stackweave_c.current_g() is not None:
            force_migrate(rounds=5)
        return _orig_show(*a, **k)
    warnings.showwarning = _show
def main():
    res = {"moved_in_warn": 0, "warned": 0, "errors": []}
    for i in range(6):
        m = mkmod(i)
        t0 = threading.get_ident()
        try:
            m.go()
        except Exception as e:
            res["errors"].append(repr(e)[:100])
        if threading.get_ident() != t0: res["moved_in_warn"] += 1
    print("A done", res, flush=True)
    wg = stackweave.WaitGroup(); wg.add(8)
    def other(i):
        warnings.warn("x%d" % i, DeprecationWarning)   # ignored category; still takes the lock
        res["warned"] += 1; wg.done()
    for i in range(8):
        stackweave_c.mn_fiber(lambda i=i: other(i), hub=i % H)
    wg.wait()
    print(res, flush=True)
    print("PASS", flush=True)
stackweave.run(H, main)

# importlib runs every finder.find_spec() under the global import lock
# (_imp.acquire_lock, a _PyRecursiveMutex owned by the OS thread).  A meta-path
# finder that does I/O (parks) and migrates; then imports on other hubs.
from prelude import *
import importlib, importlib.abc
_watchdog(30)
H = 4
class ParkingFinder(importlib.abc.MetaPathFinder):
    moved = 0
    def find_spec(self, name, path, target=None):
        if name.startswith("nosuch_") and stackweave_c.current_g() is not None:
            if force_migrate(rounds=5): ParkingFinder.moved += 1
        return None
sys.meta_path.insert(0, ParkingFinder())
def main():
    res = {"first": None, "others": 0, "errs": []}
    try:
        import nosuch_mod_a
    except ImportError as e:
        res["first"] = type(e).__name__
    except Exception as e:
        res["first"] = repr(e)[:120]
    print("after first import:", res, "moved", ParkingFinder.moved, flush=True)
    wg = stackweave.WaitGroup(); wg.add(H)
    def other(i):
        try: importlib.import_module(["colorsys", "fractions", "statistics", "difflib"][i])
        except Exception as e: res["errs"].append(repr(e)[:80])
        res["others"] += 1; wg.done()
    for i in range(H):
        stackweave_c.mn_fiber(lambda i=i: other(i), hub=i)
    wg.wait()
    print(res, flush=True); print("PASS", flush=True)
stackweave.run(H, main)

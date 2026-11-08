# multiprocessing.Value's default lock (a recursive SemLock) held across a park
# that migrates; release() checks ISMINE = (count>0 && OS thread == last_tid).
from prelude import *
patched = len(sys.argv) > 1 and sys.argv[1] == "patch"
if patched:
    import stackweave.monkey as monkey; monkey.patch()
import multiprocessing as mp
_watchdog(40)
def main():
    v = mp.Value("i", 0)
    res = {"ok": 0, "err": [], "moved": 0}
    def w():
        try:
            with v.get_lock():
                if force_migrate(rounds=20): res["moved"] += 1
                v.value += 1
            res["ok"] += 1
        except Exception as e:
            res["err"].append(repr(e)[:100])
    w()
    # is the lock still usable?
    got = v.get_lock().acquire(timeout=1)
    if got: v.get_lock().release()
    print(res, "reacquire:", got, flush=True)
    print("PASS" if res["ok"] == 1 and got else "FAIL", flush=True)
stackweave.run(2, main)

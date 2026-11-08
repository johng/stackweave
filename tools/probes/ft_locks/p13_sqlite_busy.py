# One connection per fiber on one database file.  Fiber Hd holds the write lock
# across a park; fiber Wr on the SAME hub writes and hits sqlite's busy handler,
# which sleeps inside sqlite3_step on the hub's OS thread.  Threads: Wr waits
# ~50 ms and succeeds.
# `patch` (2nd arg): monkey.patch() first, whose sqlite category moves the wait
# out of sqlite3_step (monkey/sqlite.py).
from prelude import *
if len(sys.argv) > 2 and sys.argv[2] == "patch":
    import stackweave.monkey as monkey; monkey.patch()
import sqlite3, tempfile, time as _t
H = int(sys.argv[1]) if len(sys.argv) > 1 else 2
_watchdog(30)
path = tempfile.mktemp(suffix=".db")
c0 = sqlite3.connect(path); c0.execute("create table t(x)"); c0.commit(); c0.close()
def main():
    out = {}
    wg = stackweave.WaitGroup(); wg.add(2)
    def holder():
        a = sqlite3.connect(path, timeout=2)
        a.execute("insert into t values (1)")      # implicit BEGIN: holds the write lock
        stackweave.sleep(0.05)                     # park while holding it
        a.commit(); out["holder_commit_at"] = round(_t.monotonic() - T0, 3); wg.done()
    def writer():
        b = sqlite3.connect(path, timeout=2)
        t0 = _t.monotonic()
        try:
            b.execute("insert into t values (2)"); b.commit(); out["writer"] = "ok"
        except Exception as e:
            out["writer"] = repr(e)
        out["writer_waited"] = round(_t.monotonic() - t0, 3); wg.done()
    T0 = _t.monotonic()
    stackweave_c.mn_fiber(holder, hub=0)
    stackweave.sleep(0.01)
    stackweave_c.mn_fiber(writer, hub=0)
    wg.wait()
    print(out, flush=True)
    print("PASS" if out["writer"] == "ok" and out["writer_waited"] < 1 else "FAIL", flush=True)
stackweave.run(H, main)

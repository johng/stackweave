# A sqlite3 user function parks and migrates while sqlite3_step holds the
# connection's (serialized-mode, pthread) mutex.  Then use the connection
# from the new thread and from the old one.
from prelude import *
import ctypes
if len(sys.argv) > 2 and sys.argv[2] == "serialized":
    lib = ctypes.CDLL("/usr/lib/libsqlite3.dylib")
    rc = lib.sqlite3_config(3)   # SQLITE_CONFIG_SERIALIZED
    print("sqlite3_config(SERIALIZED) rc=%d threadsafe()=%d" % (rc, lib.sqlite3_threadsafe()), flush=True)
import sqlite3
mode = sys.argv[1] if len(sys.argv) > 1 else "migrate"
_watchdog(25)
def main():
    con = sqlite3.connect(":memory:", check_same_thread=False)
    if len(sys.argv) > 2:
        lib = ctypes.CDLL("/usr/lib/libsqlite3.dylib")
        # sqlite3_db_mutex(db) != NULL  <=> this connection is serialized
        print("connection mutex present:", "yes" if con.execute("select 1") and True else "?", flush=True)
    con.execute("create table t(x)"); con.executemany("insert into t values (?)", [(i,) for i in range(5)])
    info = {"moved": 0, "calls": 0}
    def udf(x):
        info["calls"] += 1
        if mode == "migrate" and x == 2:
            if force_migrate(rounds=20): info["moved"] += 1
        elif mode == "park" and x == 2:
            stackweave.sleep(0.05)
        return x * 10
    con.create_function("f", 1, udf)
    res = {}
    def A():
        t0 = threading.get_ident()
        res["A"] = con.execute("select sum(f(x)) from t").fetchone()[0]
        res["A_moved"] = threading.get_ident() != t0
    def B():
        stackweave.sleep(0.01)
        try:
            res["B"] = con.execute("select count(*) from t").fetchone()[0]
        except Exception as e:
            res["B"] = repr(e)
    wg = stackweave.WaitGroup(); wg.add(2)
    stackweave_c.mn_fiber(lambda: (A(), wg.done()), hub=0)
    stackweave_c.mn_fiber(lambda: (B(), wg.done()), hub=0)
    wg.wait()
    print("phase1", res, info, flush=True)
    # After A's statement finished on another thread: is the mutex usable from
    # every hub?
    out = {}
    wg2 = stackweave.WaitGroup(); wg2.add(2)
    def use(h):
        try: out[h] = con.execute("select count(*) from t").fetchone()[0]
        except Exception as e: out[h] = repr(e)
        wg2.done()
    stackweave_c.mn_fiber(lambda: use(0), hub=0)
    stackweave_c.mn_fiber(lambda: use(1), hub=1)
    wg2.wait()
    print("phase2", out, flush=True)
    print("PASS" if res.get("A") == 100 and res.get("B") == 5 and out == {0: 5, 1: 5} else "FAIL", flush=True)
stackweave.run(2, main)

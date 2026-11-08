# Minimal: default sqlite3.connect() (check_same_thread=True); a user function
# parks and resumes on another hub in the middle of sqlite3_step.
from prelude import *
import ctypes
if sys.argv[1] == "serialized":
    ctypes.CDLL("/usr/lib/libsqlite3.dylib").sqlite3_config(3)   # SQLITE_CONFIG_SERIALIZED (Linux distros' default)
import sqlite3
def main():
    con = sqlite3.connect(":memory:")
    moved = []
    def udf(x):
        moved.append(force_migrate(rounds=20))
        return x
    con.create_function("f", 1, udf)
    print("result", con.execute("select f(7)").fetchone(), "moved_in_udf", moved, flush=True)
    print("again", con.execute("select 1").fetchone() if not moved[0] else "(migrated: skip, check_same_thread)", flush=True)
    print("PASS", flush=True)
stackweave.run(2, main)

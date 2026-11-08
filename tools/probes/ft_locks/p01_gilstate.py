# PyGILState_Ensure from C called inside a fiber (sqlite3 user function, ctypes callback)
from prelude import *
import sqlite3, ctypes, sys
mode = sys.argv[1]
H = int(sys.argv[2]) if len(sys.argv) > 2 else 2
def work():
    if mode == "sqlite":
        con = sqlite3.connect(":memory:", check_same_thread=False)
        con.create_function("f", 1, lambda x: x + 1)
        r = con.execute("select f(41)").fetchone()[0]
        assert r == 42, r
    elif mode == "ctypes":
        CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)
        cb = CB(lambda x: x + 1)
        qsort_arr = (ctypes.c_int * 3)(3, 1, 2)
        CMP = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int))
        libc = ctypes.CDLL(None)
        libc.qsort(qsort_arr, 3, ctypes.sizeof(ctypes.c_int), CMP(lambda a, b: a[0] - b[0]))
        assert list(qsort_arr) == [1, 2, 3]
    print("ok in fiber", flush=True)
def main():
    wg = stackweave.WaitGroup(); wg.add(4)
    def w():
        try: work()
        finally: wg.done()
    for _ in range(4): stackweave.fiber(w)
    wg.wait()
    print("PASS", flush=True)
stackweave.run(H, main)

#!/usr/bin/env python3
"""tsan_oracle_teeth.py -- teeth for the ob_ref_local oracle (tsan_refcount_oracle.py).

Plants exactly the race the oracle exists to find: a thread that does NOT own an
object updating its ob_ref_local through the owner path, while the owner does the
same.  It fakes the stale thread id by pointing the object's ob_tid at itself
around a Py_IncRef/Py_DecRef pair.  On an ORACLE=1 interpreter TSan MUST report a
plain write/write race in Py_IncRef / Py_DecRef (object.c); on a plain TSan
interpreter it is invisible (both sides are relaxed atomics) -- run it on both to
see the difference.  No stackweave involved; pure CPython.

The incidental synchronisation of free-threaded CPython (every shared refcount
RMW is a TSan release+acquire) orders most iterations, so it takes ~10^5 planted
writes for one unordered pair; a few thousand is not enough.
"""
import ctypes
import os
import threading


class Box:
    pass


x = Box()
keep = [x] * 5000                       # cushion: the planted race loses updates
OBTID = ctypes.c_uint64.from_address(id(x))   # ob_tid is the first field (FT build)
owner_tid = OBTID.value
incref, decref = ctypes.pythonapi.Py_IncRef, ctypes.pythonapi.Py_DecRef
incref.argtypes = decref.argtypes = [ctypes.c_void_p]
addr = id(x)
go = threading.Event()


def thief():
    mine = Box()
    my_tid = ctypes.c_uint64.from_address(id(mine)).value
    go.set()
    for _ in range(200000):
        OBTID.value = my_tid            # a stale id "says" this thread owns x
        incref(addr)                    # owner path, plain store, wrong thread
        decref(addr)
        OBTID.value = owner_tid


t = threading.Thread(target=thief)
t.start()
go.wait()
for _ in range(3000000):
    z = x                               # owner path on the real owner
    del z
t.join()
print("TEETH oracle ran", flush=True)
os._exit(0)                             # x's refcount is not trustworthy now

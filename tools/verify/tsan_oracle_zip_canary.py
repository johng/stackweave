#!/usr/bin/env python3
"""tsan_oracle_zip_canary.py -- is exec-home's volatile `_Py_ThreadId()` load-bearing?

Run under an ORACLE interpreter (tools/build_tsan_cpython.sh ORACLE=1 or
ORACLE=plain-tid) with the TSan ext (STACKWEAVE_TSAN=1).  docs/dev/TSAN.md has
the result table.

Without the volatile, clang 21 -O3 computes `_Py_ThreadId()` ONCE at the top of
zip_next (Python/bltinmodule.c, for _PyObject_IsUniquelyReferenced(result)) and
reuses it after `(*Py_TYPE(it)->tp_iternext)(it)` -- a call that runs arbitrary
Python.  Here that is a generator that PARKS and is woken by a foreign OS thread,
so the fiber resumes on another hub; the Py_DECREF(olditem) that follows trusts
the ORIGIN hub's id, and an olditem the origin hub owns takes the non-atomic
owner path from the wrong thread while churner fibers on the origin hub update
the same objects' ob_ref_local.  The oracle reports it as a write/write race at
bltinmodule.c `zip_next` (the Py_DECREF(olditem) line).

Expect: ORACLE=plain-tid -> a zip_next report in most runs; ORACLE=1 (the
shipped exec-home) -> none.  Prints the number of cross-hub resumes, which must
be well above zero for the run to mean anything.
"""
import os
import threading
import time

import stackweave

HUBS = int(os.environ.get("HUBS", "4"))
NZ = int(os.environ.get("NZ", "8"))           # zipping fibers
NC = int(os.environ.get("NC", "16"))          # churning fibers
ROUNDS = int(os.environ.get("ROUNDS", "200"))


class Obj:
    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v


pool = []
stop = [False]
moves = [0]


def filler(i, done):
    for k in range(32):
        pool.append(Obj(i * 100 + k))         # owned by whichever hub runs this
    done.send(1)


def churner(i):
    n = 0
    while not stop[0]:
        for o in pool:
            t = o                             # owner-path inc/dec on the true owner
        n += 1
        if n % 4 == 0:
            stackweave.sleep(0)


def parking_gen(ch, i):
    k = 0
    while True:
        yield pool[(i * 7 + k) % len(pool)]
        k += 1

        def poke(v=k):
            time.sleep(0.0002)
            while True:
                try:
                    ch.send(v)
                    return
                except RuntimeError:          # receiver not parked yet
                    time.sleep(0.0001)
        threading.Thread(target=poke, daemon=True).start()
        before = threading.get_ident()
        ch.recv()                             # park; foreign wake -> global run-queue
        if threading.get_ident() != before:
            moves[0] += 1


def zipper(i, out):
    ch = stackweave.Chan(0)
    for _a, _b in zip(parking_gen(ch, i), range(ROUNDS)):
        pass
    out.send(1)


def main():
    done = stackweave.Chan(64)
    for i in range(HUBS * 2):
        stackweave.fiber(filler, i, done)
    for _ in range(HUBS * 2):
        done.recv()
    for i in range(NC):
        stackweave.fiber(churner, i)
    out = stackweave.Chan(NZ)
    for i in range(NZ):
        stackweave.fiber(zipper, i, out)
    for _ in range(NZ):
        out.recv()
    stop[0] = True
    stackweave.sleep(0.05)
    print("zip rounds done, cross-hub resumes: %d" % moves[0], flush=True)


stackweave.run(HUBS, main)

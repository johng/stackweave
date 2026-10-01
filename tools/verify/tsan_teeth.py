#!/usr/bin/env python3
"""tsan_teeth.py -- planted-race teeth control for the TSan lane (docs/dev/TSAN.md).

Run under the TSan-built ext (STACKWEAVE_TSAN=1) on a --with-thread-sanitizer
interpreter, one mode per process:

  race   two fibers on two hubs write the same byte of a shared bytearray
         through their own memoryviews with nothing ordering them.  TSan MUST
         report a data race (memoryobject.c pack_single).  If it does not,
         either the interpreter is not TSan-built or the fiber annotations
         (runloom_fiber_san.h) have merged the two hubs' histories.

  clean  the same byte, written by fibers that take turns through a channel,
         and by one fiber before and after a forced cross-hub migration (a
         foreign OS thread wakes it, so it resumes on whichever hub pulls the
         global run-queue).  TSan MUST stay silent: a report here means the
         annotations lost the happens-before edge a park/wake or a migration
         carries -- a false-positive generator for every other run.

Prints "TEETH <mode> ran", plus "cross-hub" when the writers really ran on two
OS threads (race) or the clean writer really moved; the harness judges by the
TSan log.

Why a memoryview store: it is a plain C store into the buffer with no lock and
no atomic in between.  Anything that does an atomic read-modify-write on a
SHARED object each iteration (a ctypes call through a shared function object,
an incref of a shared non-immortal object) is a TSan release+acquire pair, and
those pairs order the two loops often enough to hide the race -- the planted
race has to avoid them, and so does any real race TSan is expected to find.
"""
import os
import sys
import threading
import time

import stackweave

ba = bytearray(64)


def race():
    # Both writers can land on one hub (then the first spins out its deadline and
    # they run one after the other, which is no race).  Retry until they run on
    # two OS threads at once; the harness requires "cross-hub".
    for _attempt in range(8):
        tids = []
        done = stackweave.Chan(2)

        def writer(value):
            mv = memoryview(ba)      # this fiber's own view: no shared refcount
            tids.append(threading.get_ident())
            deadline = time.monotonic() + 2.0
            while len(tids) < 2 and time.monotonic() < deadline:
                pass                 # both writers live before either writes
            if len(set(tids)) == 2:
                for _ in range(100000):
                    mv[0] = value
            mv.release()
            done.send(1)

        stackweave.fiber(writer, 65)
        stackweave.fiber(writer, 66)
        done.recv()
        done.recv()
        if len(set(tids)) == 2:
            break
    print("TEETH race ran%s" % (" cross-hub" if len(set(tids)) == 2 else ""),
          flush=True)


def clean():
    # (1) Turn-taking through an unbuffered channel: every write is ordered
    # after the previous one by the hand-off.
    ping, pong = stackweave.Chan(0), stackweave.Chan(0)
    finished = stackweave.Chan(2)

    def a():
        mv = memoryview(ba)
        for i in range(200):
            mv[0] = 65
            ping.send(i)
            pong.recv()
        mv.release()
        finished.send(1)

    def b():
        mv = memoryview(ba)
        for i in range(200):
            ping.recv()
            mv[0] = 66
            pong.send(i)
        mv.release()
        finished.send(1)

    stackweave.fiber(a)
    stackweave.fiber(b)
    finished.recv()
    finished.recv()

    # (2) One fiber writes, parks until a FOREIGN thread wakes it (no deque, so
    # the wake goes through the global run-queue and may land on another hub),
    # and writes again.  Program order across the migration must be an edge,
    # and so must the hand-off to the next round's poker thread.
    ch = stackweave.Chan(0)
    mv = memoryview(ba)
    moved = 0
    for i in range(400):             # until a few wakes resumed on another hub
        if moved >= 3:
            break
        before = threading.get_ident()
        mv[0] = 67

        def poke(i=i):
            time.sleep(0.001)
            while True:
                try:
                    ch.send(i)
                    return
                except RuntimeError:   # no receiver parked yet
                    time.sleep(0.0005)

        t = threading.Thread(target=poke, daemon=True)
        t.start()
        ch.recv()
        t.join()
        mv[0] = 68
        if threading.get_ident() != before:
            moved += 1
    mv.release()
    print("TEETH clean ran%s" % (" cross-hub" if moved else ""), flush=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "race"
    stackweave.run(int(os.environ.get("TEETH_HUBS", "2")),
                   race if mode == "race" else clean)

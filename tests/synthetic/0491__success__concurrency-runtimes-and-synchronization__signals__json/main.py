# -*- coding: utf-8 -*-
"""Concurrency runtimes and synchronization -- a concurrency runtimes and synchronization toy using the signals primitive with a json payload, expecting success.

Synthetic stackweave toy program (auto-generated).
  test type : success
  category  : concurrency runtimes and synchronization
  primitive : signals
  format    : json (json)
  scheduler : M:N via stackweave.run(8, root), free-threaded 3.13t, GIL off

Exercises stackweave's main API -- the root goroutine spawns workers with
stackweave.fiber(...) onto 8 hub threads, using the monkey-patched cooperative
signals primitive to carry a json payload.  Prints PASS and exits 0 when
healthy; FAIL / hang / crash signals a bug.
"""
import sys
import os
import io
import time
import json
import zlib
import base64
import struct
import csv
import pickle
import hashlib
import tempfile
import shutil
import socket
import ssl
import selectors
import signal
import subprocess
import threading
import queue
import multiprocessing as mp

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))
import stackweave
import stackweave_c

THEME = "concurrency runtimes and synchronization"
CATSLUG = "concurrency-runtimes-and-synchronization"
PRIM = "signals"
FMT = "json"
TESTTYPE = "success"
NW = 3
NHUB = 8
CERT = os.path.join(HERE, "..", "_assets", "cert.pem")
KEY = os.path.join(HERE, "..", "_assets", "key.pem")
PY = sys.executable


def finish(ok, info=""):
    if ok:
        print("PASS")
    else:
        print("FAIL:", info)
        sys.exit(1)


def make_coordinator(results, n, state):
    """Goroutine that fans in n boolean results over a channel."""
    def coordinator():
        good = 0
        for _ in range(n):
            value, _ = results.recv()
            if value:
                good += 1
        state["good"] = good
    return coordinator


def recvexact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("short read")
        buf += chunk
    return bytes(buf)

# ---- format codec ----
def mk_payload():
    return {"category": THEME, "kind": "json",
            "items": [THEME + "#" + str(i) for i in range(8)],
            "count": 8, "ok": True, "ratio": 0.5}

def encode(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")

def decode(buf):
    return json.loads(bytes(buf).decode("utf-8"))

# ---- body ----
def main():
    stackweave.monkey.patch()
    GO = stackweave.fiber
    payload = mk_payload()
    enc = encode(payload)
    fmt_ok = decode(enc) == payload
    sig = signal.SIGUSR1
    state = {"hit": False}

    def handler(signum, frame):
        state["hit"] = True

    previous = signal.signal(sig, handler)      # install on main thread

    def waiter():
        for _ in range(400):
            if state["hit"]:
                return
            stackweave.sleep(0.005)

    def raiser():
        stackweave.sleep(0.02)
        os.kill(os.getpid(), sig)

    def __root():
        GO(waiter)
        GO(raiser)
    stackweave.run(NHUB, __root)
    signal.signal(sig, previous)
    finish(fmt_ok and state["hit"], state)

if __name__ == "__main__":
    main()

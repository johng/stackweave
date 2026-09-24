#!/usr/bin/env python3
"""Exp F: steady-state spawn/complete CHURN at bounded concurrency.

Unlike scaling.py (spawn N up front, then drain = a one-shot burst), this keeps a
roughly fixed live-set churning: `conc` chains, each fiber spawning its successor
right before it returns, `depth = n/conc` deep.  At any instant ~conc fibers are
live and the pooled stacks cycle spawn->complete->reuse -- the regime where Go's
warm-stack free-list pays off.  This is the realistic server shape (handle req ->
spawn handler -> complete -> next).

Measures total completions / wall."""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
import stackweave


def worker(d):
    if d > 1:
        stackweave.fiber(worker, d - 1)        # spawn successor, then return -> slot frees


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hubs", type=int, default=8)
    ap.add_argument("--conc", type=int, default=2000, help="bounded live concurrency")
    ap.add_argument("--n", type=int, default=300000, help="total completions")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--label", default="churn")
    args = ap.parse_args()

    depth = max(1, args.n // args.conc)
    total = depth * args.conc

    def root():
        for _ in range(args.conc):
            stackweave.fiber(worker, depth)

    best = 1e18
    for _ in range(args.reps):
        t0 = time.perf_counter()
        stackweave.run(args.hubs, root)
        best = min(best, time.perf_counter() - t0)

    rate = total / best
    rec = {"label": args.label, "hubs": args.hubs, "conc": args.conc, "n": total,
           "reps": args.reps, "seconds": best, "churn_per_s": rate}
    print("%-20s conc=%-6d %9.0f churn/s  (%.3fs / %d)" %
          (args.label, args.conc, rate, best, total), file=sys.stderr)
    print(json.dumps(rec))


if __name__ == "__main__":
    main()

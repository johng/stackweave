#!/usr/bin/env python3
"""covering.py -- t-way combinatorial interaction testing of stackweave's config matrix.

stackweave has a combinatorial explosion of runtime knobs -- steal randomization x
ready-ring fairness x idle backoff x sysmon x ... -- and bugs love to hide
in *interactions* between them, not in any single setting. Testing the full
cartesian product is wasteful; testing one-factor-at-a-time misses interactions
entirely.

Combinatorial interaction testing splits the difference: a **t-way covering
array** is a small set of configurations in which *every* combination of t
factor-values appears at least once. Empirically the large majority of real
interaction bugs are triggered by <=2 or <=3 factors, so a pairwise (t=2) or
3-way array catches them with a tiny fraction of the runs.
  Kuhn, Wallace, Gallo, "Software Fault Interactions and Implications for
  Software Testing", IEEE TSE 2004 (the "most bugs are <=6-way, most of those
  <=3-way" result). Cohen et al, AETG, for the greedy construction here.

The array is built by a randomized-greedy AETG-style algorithm (pure stdlib).
Each generated configuration is then exercised by running the M:N scheduler
fuzzer (tools/mn_stress.py) under that environment; any failure is reported
with the exact env to reproduce.

House style: .format(), no f-strings.

Usage:
  tools/combinatorial/covering.py --list            # show the array + stats only
  tools/combinatorial/covering.py [--t 2] [--iters 50] [--seed 0]
"""
import argparse
import itertools
import os
import random
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

# Linux, GIL-off (PYTHON_GIL=0) factor matrix.  PYTHON_GIL is fixed at 0: the
# M:N scheduler is only real with the GIL off, and GIL=1 + unbuffered self-
# select is a separately-tracked known deadlock, so we don't fold it in here.
#
# These are the scheduler knobs that still change M:N behaviour.  Cross-hub
# migration, preemption and the netpoll backend are no longer switchable, so the
# matrix is built from what remains:
#   SCHED_RANDOM        randomized steal victim + spawn placement (default off
#                       in release, on under a sanitizer build)
#   READY_STARVE_BOUND  0 = ready ring always before the deque; 64 = default
#                       fairness turn after 64 ready-ring services
#   IDLE_BACKOFF_MS     1 = no idle backoff (1ms re-pump); 32 = default cap
#   SYSMON              sysmon always runs; this only toggles its WEDGED lines
FACTORS = [
    ("STACKWEAVE_SCHED_RANDOM",       ["0", "1"]),
    ("STACKWEAVE_READY_STARVE_BOUND", ["0", "64"]),
    ("STACKWEAVE_IDLE_BACKOFF_MS",    ["1", "32"]),
    ("STACKWEAVE_SYSMON",             ["0", "1"]),
]


def all_ttuples(factors, t):
    """Every t-way combination that a covering array must include.

    A tuple is (((fi, value), ...)) over a size-t subset of factor indices.
    """
    tuples = set()
    for combo in itertools.combinations(range(len(factors)), t):
        value_lists = [factors[fi][1] for fi in combo]
        for values in itertools.product(*value_lists):
            tuples.add(tuple((combo[k], values[k]) for k in range(t)))
    return tuples


def newly_covered(row, fi, value, partial_idxs, t, uncovered):
    """How many uncovered t-tuples assigning factor fi=value would complete,
    given the already-assigned factor indices in `partial_idxs`."""
    gain = 0
    for others in itertools.combinations(partial_idxs, t - 1):
        members = sorted(list(others) + [fi])
        tup = tuple((m, value if m == fi else row[m]) for m in members)
        if tup in uncovered:
            gain += 1
    return gain


def row_covers(row, t, uncovered):
    covered = set()
    for combo in itertools.combinations(range(len(row)), t):
        tup = tuple((fi, row[fi]) for fi in combo)
        if tup in uncovered:
            covered.add(tup)
    return covered


def covering_array(factors, t=2, candidate_tries=60, seed=0):
    """Randomized-greedy (AETG-style) t-way covering array."""
    rng = random.Random(seed)
    uncovered = all_ttuples(factors, t)
    total = len(uncovered)
    rows = []
    while uncovered:
        best_row, best_cover = None, -1
        for _ in range(candidate_tries):
            order = list(range(len(factors)))
            rng.shuffle(order)
            row = {}
            assigned = []
            for fi in order:
                levels = factors[fi][1]
                if len(assigned) < t - 1:
                    pick = rng.choice(levels)
                else:
                    best_v, best_g = levels[0], -1
                    for v in levels:
                        g = newly_covered(row, fi, v, assigned, t, uncovered)
                        if g > best_g:
                            best_g, best_v = g, v
                    pick = best_v
                row[fi] = pick
                assigned.append(fi)
            cover = len(row_covers(row, t, uncovered))
            if cover > best_cover:
                best_cover, best_row = cover, dict(row)
        rows.append([best_row[i] for i in range(len(factors))])
        uncovered -= row_covers(best_row, t, uncovered)
    return rows, total


def run_config(values, iters, factors):
    """Run mn_stress under this config; return (ok, detail)."""
    env = dict(os.environ)
    env["PYTHON_GIL"] = "0"
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    for (name, _), v in zip(factors, values):
        env[name] = v
    cmd = [sys.executable, os.path.join(ROOT, "tools", "mn_stress.py"),
           "--iters", str(iters), "--stable"]
    p = subprocess.run(cmd, env=env, cwd=ROOT, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=120)
    out = p.stdout.decode(errors="replace")
    ok = (p.returncode == 0) and ("CLEAN" in out)
    if p.returncode < 0:
        detail = "signal {} (crash)".format(-p.returncode)
    else:
        detail = out.strip().splitlines()[-1] if out.strip() else "(no output)"
    return ok, detail


def fmt_config(values, factors):
    return "  ".join("{}={}".format(name, v)
                     for (name, _), v in zip(factors, values))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--t", type=int, default=2, help="interaction strength")
    ap.add_argument("--iters", type=int, default=50, help="mn_stress iters/config")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--list", action="store_true", help="print array + stats, don't run")
    args = ap.parse_args()

    factors = list(FACTORS)

    rows, total_tuples = covering_array(factors, t=args.t, seed=args.seed)
    full = 1
    for _, levels in factors:
        full *= len(levels)

    print("{}-way covering array over {} factors".format(args.t, len(factors)))
    print("  full cartesian : {} configs".format(full))
    print("  covering array : {} configs  ({:.0f}% reduction, all {} {}-tuples covered)".format(
        len(rows), 100.0 * (1 - len(rows) / float(full)), total_tuples, args.t))
    print("-" * 72)
    for i, values in enumerate(rows):
        print("  [{:>2}] {}".format(i, fmt_config(values, factors)))
    print("-" * 72)

    if args.list:
        return 0

    print("running mn_stress ({} iters) under each config:".format(args.iters))
    failures = []
    for i, values in enumerate(rows):
        try:
            ok, detail = run_config(values, args.iters, factors)
        except subprocess.TimeoutExpired:
            ok, detail = False, "TIMEOUT/HANG"
        mark = "ok  " if ok else "FAIL"
        print("  [{:>2}] {}  {}".format(i, mark, detail if not ok else ""))
        if not ok:
            failures.append((values, detail))

    print("-" * 72)
    if failures:
        print("{} config(s) FAILED -- reproduce with:".format(len(failures)))
        for values, detail in failures:
            env = " ".join("{}={}".format(n, v) for (n, _), v in zip(factors, values))
            print("  PYTHON_GIL=0 PYTHONPATH=src {} python tools/mn_stress.py "
                  "--iters {} --stable    # {}".format(env, args.iters, detail))
        return 1
    print("all {} configs CLEAN".format(len(rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

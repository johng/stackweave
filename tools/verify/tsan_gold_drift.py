#!/usr/bin/env python3
"""tsan_gold_drift.py -- has the GOLD-STANDARD TSan run gone stale?

There are two TSan tiers (tools/run_sanitizers_ext.sh):

  ext-only   instruments only stackweave_c and force-loads libtsan into a stock
             interpreter.  Cheap, needs no special CPython, and is BLIND to
             the interpreter's internals -- so a race that crosses the
             ext <-> CPython boundary is attributed poorly or not at all.
  gold       STACKWEAVE_TSAN_PYTHON=<a --with-thread-sanitizer CPython>.  Both
             sides instrumented, so cross-boundary races land precisely.

Gold is the one that can go quietly stale, because running it needs an
interpreter most checkouts do not have -- and a claim of "TSan-clean" ages
badly without anything to say so.  That is not hypothetical: the header of
run_sanitizers_ext.sh carried "Verified: stackweave's C is TSan-clean under it"
from a run against **3.13t**, while RUNLOOM_GCFRAMES_ANCHOR is gated
`Py_GIL_DISABLED && PY_VERSION_HEX >= 0x030E0000` -- 3.14+ ONLY.  The whole
GC-frames anchor compiles to nothing on 3.13, so the clean bill of health had
never seen that code at all.  An ext-only run on 3.14 later found data races
in it (module_gcframes.c.inc, runloom_sched_pystate.c.inc).

WHY CONTENT *AND* A THRESHOLD.  Two failure modes to avoid, pulling opposite
ways:

  Keying on commit count alone fires on a docs typo and stays silent through
  a scheduler rewrite landed as one commit.  So the sources TSan actually
  instruments (src/runloom_c) must have CHANGED for this to say anything --
  the same way model_source_drift keys on anchored function bodies.

  But content alone would warn on EVERY commit that touches C, and a full
  gold run per commit is absurd -- it needs a purpose-built interpreter and
  takes minutes.  A warning that appears every time is one people learn to
  scroll past, which is the disease this tool exists to treat.

So a change is necessary but not sufficient: it also takes GOLD_COMMITS
commits touching src/runloom_c, or GOLD_DAYS since the last gold run.  Both
are deliberately generous.  The question being asked is "has this drifted far
enough to be worth a purpose-built interpreter", not "is it identical".

WARNS, NEVER FAILS.  Exit is always 0.  A developer without a TSan CPython
must still be able to get a green gate, and a check that reliably fails for
an unactionable reason is one people learn to ignore -- which is the disease
this tool exists to treat, not spread.

Usage:
  tsan_gold_drift.py             # warn if the instrumented sources moved
  tsan_gold_drift.py --update    # record HEAD as gold-verified (run AFTER a
                                 # clean gold run)
  tsan_gold_drift.py --json
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC_C = os.path.join(ROOT, "src", "stackweave_c")
BASELINE = os.path.join(HERE, "tsan_gold_baseline.json")

# What TSan actually instruments when it builds the extension.
SRC_EXTS = (".c", ".c.inc", ".h", ".S")

# Hysteresis. A gold run needs a --with-thread-sanitizer CPython and takes
# minutes, so it is a "periodically, and after real churn" job -- not a
# per-commit one. These are the thresholds a CHANGED src/runloom_c must also
# clear before this says anything. Tune them here rather than in the caller.
GOLD_COMMITS = 10      # commits touching src/runloom_c since the last gold run
GOLD_DAYS = 30         # ...or this long since it, whichever trips first


def _iter_sources():
    for dirpath, dirnames, filenames in os.walk(SRC_C):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(SRC_EXTS):
                yield os.path.join(dirpath, name)


def sources_hash():
    """One hash over every instrumented source, path-sensitive so a rename or
    a deletion counts as a change."""
    h = hashlib.sha256()
    for path in _iter_sources():
        h.update(os.path.relpath(path, ROOT).encode())
        h.update(b"\0")
        with open(path, "rb") as fh:
            h.update(fh.read())
        h.update(b"\0")
    return h.hexdigest()[:16]


def _git(*args):
    try:
        return subprocess.check_output(("git",) + args, cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def commits_touching_src_since(commit):
    """Colour only: how many commits since the baseline touched src/runloom_c."""
    if not commit:
        return None
    out = _git("rev-list", "--count", "%s..HEAD" % commit, "--", "src/runloom_c")
    try:
        return int(out)
    except ValueError:
        return None


def days_since(iso_date):
    """Whole days since an ISO-8601 date, or None if unparseable. Half of the
    hysteresis: a tree that barely changes should still get re-verified
    occasionally, because the INTERPRETER moves underneath it too -- which is
    exactly how the last 'TSan-clean' claim went stale across 3.13 -> 3.14."""
    if not iso_date:
        return None
    try:
        import datetime as _dt
        then = _dt.datetime.fromisoformat(iso_date)
        now = _dt.datetime.now(then.tzinfo) if then.tzinfo else _dt.datetime.now()
        return max(0, (now - then).days)
    except Exception:
        return None


def load_baseline():
    if not os.path.exists(BASELINE):
        return {}
    try:
        with open(BASELINE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true",
                    help="record HEAD as gold-verified (after a clean gold run)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    cur = sources_hash()
    head = _git("rev-parse", "HEAD")

    if a.update:
        data = {
            "sources_hash": cur,
            "commit": head,
            "date": _git("log", "-1", "--format=%cI") or "",
            "python": os.environ.get("STACKWEAVE_TSAN_PYTHON", "") or "(unrecorded)",
            "note": ("Set by tsan_gold_drift.py --update after a clean "
                     "gold-standard run (tools/run_sanitizers_ext.sh with "
                     "STACKWEAVE_TSAN_PYTHON). Records WHICH sources were "
                     "verified, so the warning keys on content rather than "
                     "commit count."),
        }
        with open(BASELINE, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.write("\n")
        print("tsan_gold_drift: recorded gold-verified at %s (sources %s)"
              % (head[:12] or "?", cur))
        return 0

    base = load_baseline()
    changed = base.get("sources_hash") != cur
    n = commits_touching_src_since(base.get("commit"))
    days = days_since(base.get("date"))

    # Changed is necessary but NOT sufficient -- see the module docstring.
    over_commits = n is not None and n >= GOLD_COMMITS
    over_days = days is not None and days >= GOLD_DAYS
    warn = (not base) or (changed and (over_commits or over_days))

    if a.json:
        print(json.dumps({"warn": warn, "changed": changed,
                          "sources_hash": cur, "baseline": base,
                          "commits_touching_src": n, "days_since": days}))
        return 0

    if not base:
        print("tsan_gold_drift: WARNING -- the gold-standard TSan run has NEVER "
              "been recorded.")
    elif not changed:
        print("tsan_gold_drift: OK -- gold-standard TSan verified these exact "
              "sources (%s)" % cur)
        return 0
    elif not warn:
        # Drifted, but not far enough to be worth a purpose-built interpreter.
        # Say so in one quiet line rather than nagging: the point is that this
        # is UNDER the threshold, not that nothing has changed.
        print("tsan_gold_drift: OK -- src/runloom_c changed since the last gold "
              "run (%s commit(s), %s day(s)); under the %d-commit / %d-day "
              "threshold." % (n if n is not None else "?",
                              days if days is not None else "?",
                              GOLD_COMMITS, GOLD_DAYS))
        return 0
    else:
        print("tsan_gold_drift: WARNING -- src/runloom_c has drifted well past "
              "the last gold-standard TSan run.")
        print("  last verified : %s (%s)" % (base.get("commit", "?")[:12],
                                             base.get("date", "?")))
        print("  interpreter   : %s" % base.get("python", "(unrecorded)"))
        print("  since then    : %s commit(s) touching src/runloom_c, %s day(s)"
              % (n if n is not None else "?", days if days is not None else "?"))
        print("  threshold     : %d commits or %d days"
              % (GOLD_COMMITS, GOLD_DAYS))

    print("")
    print("  The ext-only TSan lane instruments stackweave_c but NOT the")
    print("  interpreter, so races crossing the ext <-> CPython boundary are")
    print("  attributed poorly. Gold instruments both. Run it with:")
    print("")
    print("    tools/build_tsan_cpython.sh          # once; PY_VER matters --")
    print("                                         # build the version you SHIP")
    print("    STACKWEAVE_TSAN_PYTHON=~/cpython-tsan/bin/python3 \\")
    print("      tools/run_sanitizers_ext.sh")
    print("    tools/verify/tsan_gold_drift.py --update   # after it comes back clean")
    print("")
    print("  (warning only -- this never fails the gate)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

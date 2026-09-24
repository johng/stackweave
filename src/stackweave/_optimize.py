"""stackweave.optimize() -- pick the trade-off(s) you care about and the runtime
leans that way.

Call nothing and you get the defaults; call optimize() with one or more *named
trades* and the runtime leans that way.  Each name says what you are spending
and buying:

    stackweave.optimize()                          # nothing -- keep the defaults
    stackweave.optimize("throughput")              # max spawn rate (spends RAM)
    stackweave.optimize("memory")                  # right-sized stacks (spends spawn rate)
    stackweave.optimize("latency")                 # sharp tail  (spends a little CPU)
    stackweave.optimize("secure")                  # hardened    (spends a little speed)
    stackweave.optimize("throughput", "latency")   # compose -- pass the trades you want
    stackweave.optimize("memory", max_fibers=200_000)

Natural synonyms work too -- ``optimize("speed")`` == ``optimize("throughput")``,
``optimize("rss")`` == ``optimize("memory")`` (case-insensitive).

What each trade does:

    throughput  ``stackweave.fiber`` spawns like ``fiber_fast`` (max naked-spawn
                rate, fixed default stack), and the blocking-offload pool gets
                16 workers (STACKWEAVE_BLOCKPOOL_WORKERS).
    memory      ``stackweave.fiber`` uses the grow-down auto-sizer (small
                right-sized stacks) -- the default, re-asserted.
    latency     sysmon's wedge budget drops to 25 ms (STACKWEAVE_SYSMON_MS):
                faster recovery from a wedged hub for a few hundred extra
                wakeups/sec.
    secure      recycled fiber stacks are wiped (``set_stack_scrub(True)``).
    max_fibers  a hard ceiling on live fibers (``inspect.set_max_fibers``).

Conflicts resolve by precedence ``secure > memory > latency > throughput``; the
only knob two trades share is the spawn path, so ``optimize("throughput",
"memory")`` keeps the grow-down auto-sizer.

CALL IT BEFORE ``stackweave.run()``.  The spawn path, stack scrub and fiber cap
apply immediately; the two numeric knobs are environment variables the runtime
reads as it starts, set only if not already set -- so an explicit shell export
of the same variable wins, and the first optimize() call wins for each.  The
returned dict reports the effective value of everything the call touched.
"""
import os

import stackweave_c

from .runtime import set_grow_down

# Each goal -> the numeric tuning env vars it sets.  Values are in the exact
# format the C runtime parses (verified against the getenv sites).
_GOAL_ENV = {
    "throughput": {
        "STACKWEAVE_BLOCKPOOL_WORKERS":         "16",      # more blocking-offload workers
    },
    "latency": {
        # Tighter stall detection -> faster recovery from a wedged hub. Only the
        # watchdog (default-on on free-threaded builds) acts on it; a no-op, never
        # a hazard, elsewhere. Costs a few hundred extra wakeups/sec -> CPU, not RAM.
        "STACKWEAVE_SYSMON_MS":                 "25",
    },
    "memory": {},
    "secure": {},
}

# Apply order = ascending precedence; the later one wins on a conflicting key.
_PRECEDENCE = ("throughput", "latency", "memory", "secure")

# Friendly synonyms -> canonical goal.  Case-insensitive; lets the natural words
# ("speed", "rss") map onto the trade names without a second vocabulary.
_ALIASES = {
    "speed": "throughput", "cpu": "throughput", "fast": "throughput",
    "time": "throughput",
    "rss": "memory", "ram": "memory", "small": "memory", "space": "memory",
    "mem": "memory",
    "tail": "latency",
    "security": "secure", "hardened": "secure", "harden": "secure",
}


def _normalize(g):
    s = str(g).strip().lower()
    return _ALIASES.get(s, s)

#: the valid trade names, in precedence order.
GOALS = tuple(_PRECEDENCE)


def optimize(*goals, max_fibers=None):
    """Tune stackweave for the trade-off(s) you care about.  Call before run().

    goals: zero or more of "throughput", "latency", "memory", "secure" -- they
        compose, and a higher-precedence goal (secure > memory > latency >
        throughput) wins on any conflicting knob.  No goals = leave the
        defaults in place.
    max_fibers: optional hard ceiling on concurrent fibers (backpressure); there
        is no sane automatic value for this, so it stays explicit.

    Returns a dict of the EFFECTIVE settings for the knobs it touched: env var
    name -> value for the numeric knobs (an explicit shell export shows through
    here, since it wins), plus "spawn" ("fast" / "grow_down"), "stack_scrub"
    and "max_fibers" for the settings applied live.
    """
    goals = tuple(_normalize(g) for g in goals)
    for g in goals:
        if g not in _GOAL_ENV:
            raise ValueError(
                "unknown optimize goal {0!r}; choose from {1}".format(
                    g, ", ".join(GOALS)))

    merged = {}
    for g in _PRECEDENCE:
        if g in goals:
            merged.update(_GOAL_ENV[g])

    # setdefault: an explicit shell env var (or an earlier optimize() call) wins.
    for k, v in merged.items():
        os.environ.setdefault(k, v)
    applied = {k: os.environ.get(k) for k in merged}

    # Spawn-path trade, applied LIVE (it picks which C entry stackweave.fiber uses):
    #   throughput -> fiber_fast: max naked-spawn rate, fixed default stack.
    #   memory     -> grow-down : small right-sized resident stacks.
    # memory > throughput, so on a conflict the leaner choice wins.  Untouched
    # unless one of the two is requested, so optimize("latency")/optimize() leave
    # stackweave.fiber at its grow-down default.
    if "throughput" in goals or "memory" in goals:
        want_speed = ("throughput" in goals) and ("memory" not in goals)
        if not want_speed:
            set_grow_down(True)
        stackweave_c._fiber_set_speed(1 if want_speed else 0)
        applied["spawn"] = "fast" if want_speed else "grow_down"

    if "secure" in goals:
        stackweave_c.set_stack_scrub(True)
        applied["stack_scrub"] = bool(stackweave_c.get_stack_scrub())

    if max_fibers is not None:
        stackweave_c.set_max_fibers(int(max_fibers))
        applied["max_fibers"] = stackweave_c.get_max_fibers()

    return applied

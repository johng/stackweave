"""stackweave.optimize() -- pick the trade-off(s) you care about and the runtime
sets the underlying knobs for you.

The point: you should never have to learn the raw RUNLOOM_* tuning env vars.
Call nothing and you get smart automatic defaults; call optimize() with one or
more *named trades* and the runtime leans that way.  Each name says exactly what
you are spending and buying:

    stackweave.optimize()                          # auto -- smart defaults (the default)
    stackweave.optimize("throughput")              # max req/s   (spends RAM)
    stackweave.optimize("memory")                  # tight RSS   (spends throughput)
    stackweave.optimize("latency")                 # sharp tail  (spends a little CPU)
    stackweave.optimize("secure")                  # hardened    (spends a little speed)
    stackweave.optimize("throughput", "latency")   # compose -- pass the trades you want
    stackweave.optimize("memory", max_fibers=200_000)

Natural synonyms work too -- ``optimize("speed")`` == ``optimize("throughput")``,
``optimize("rss")`` == ``optimize("memory")`` (case-insensitive).

The throughput/memory trades ALSO pick which spawn path ``stackweave.fiber`` uses:
``throughput`` points it at ``fiber_fast`` (max naked-spawn rate, fixed default
stack), ``memory`` at the grow-down auto-sizer (small right-sized stacks).  The
default (no call) is grow-down.

Conflicts resolve by precedence ``secure > memory > latency > throughput`` (so
``optimize("throughput", "memory")`` keeps RSS lean where they disagree).

CALL IT ONCE, BEFORE ``stackweave.run()`` -- the settings are read as the runtime
starts, and the first call wins for any given knob.  An explicit shell env var
(e.g. you exported STACKWEAVE_STACK_MADV) still wins over optimize(), so power users
keep full control; the returned dict reflects the EFFECTIVE values after that.

These trades are deliberately SAFE: each is validated (the spawn fast-path in
"throughput" -- warm-stack arena + bulk/FRESH -- is measured and gate-checked in
docs/dev/spawn_experiments.md) and none can OOM-kill a RAM-tight host on its own.
"throughput" does spend RAM (it holds freed stacks warm); compose it with "memory"
(higher precedence) to claw that back on a tight host.  The sharpest raw expert
tricks (e.g. STACKWEAVE_STACK_MADV=off) stay raw env vars with their own warnings --
a friendly name should never hide a footgun.
"""
import os

# Each goal -> the env-var bundle it pulls.  Values are in the exact format the C
# runtime parses (verified against the getenv sites).  Kept conservative: every
# value here is safe to request without a hidden OOM / experimental-mode footgun.
_GOAL_ENV = {
    "throughput": {
        "STACKWEAVE_TCPCONN_IOURING":           "auto",   # flip epoll->io_uring as conns climb
        "STACKWEAVE_TCPCONN_IOURING_THRESHOLD": "512",
        "STACKWEAVE_BLOCKPOOL_WORKERS":         "16",      # more blocking-offload workers
        # Spawn fast-path (validated in docs/dev/spawn_experiments.md): the per-size
        # stack arena keeps freed stacks warm (no per-spawn mmap/mprotect -> 8x on
        # naked spawn), and bulk+FRESH builds a big fiber_n batch in one locked op
        # and faults the frames across the hubs in parallel (~3.3x: 804k/s @8 here).
        # Costs RAM (warm stacks held resident) -- the "memory" trade turns it back off.
        "STACKWEAVE_STACK_ARENA":               "1",       # per-size warm-stack arena
        "STACKWEAVE_GON_BULK":                  "1",       # bulk-arena spawn for big fiber_n
        "STACKWEAVE_GON_FRESH":                 "1",        # defer frame-fault to first resume (parallel)
        "STACKWEAVE_GON_PCREATE":               "auto",    # parallel bulk-create (1 builder/hub) -> 1.4M+ spawn/s, TSan-clean
        "STACKWEAVE_GON_PCREATE_B":             "auto",    # parallel Pass-B coro-fill -> ~1.8-2.2M (past Go), TSan-clean
        "STACKWEAVE_PREWARM_KEEP":              "1",       # continuous depot top-up daemon
        "STACKWEAVE_HOT_HANDLERS":              "1",       # @stackweave.hot active (per-core handler copies)
        "STACKWEAVE_HOT_AUTO":                  "1",       # auto-promote the busiest handlers, no decorator
        # depot pool size is now AUTO -- it sizes itself to the live-fiber
        # high-water (vm.max_map_count- and RAM-clamped), so no static cap here.
    },
    "latency": {
        # Tighter stall detection -> faster recovery from a wedged hub. Only the
        # watchdog (default-on on free-threaded builds) acts on it; a no-op, never
        # a hazard, elsewhere. Costs a few hundred extra wakeups/sec -> CPU, not RAM.
        "STACKWEAVE_SYSMON_MS":                 "25",
    },
    "memory": {
        "STACKWEAVE_STACK_MADV":                "dontneed",  # eager reclaim, tightest RSS
        "STACKWEAVE_STACK_PARK_DONTNEED":       "1",          # return idle parked-fiber pages now
        "STACKWEAVE_GROW_DOWN":                 "1",          # per-function stack learning (M:N)
        "STACKWEAVE_STACK_ARENA":               "0",          # no warm-stack arena (don't hold RSS); precedence > throughput
        "STACKWEAVE_STACK_SCRUB_RESIDENT":      "0",          # DONTNEED scrub reclaims pages (resident-memset holds them)
        "STACKWEAVE_HOT_HANDLERS":              "0",          # no per-core handler copies (spend the RAM back)
        "STACKWEAVE_HOT_AUTO":                  "0",          # and don't auto-promote either
    },
    "secure": {
        "STACKWEAVE_STACK_SCRUB":               "1",       # wipe recycled stacks (TLS keys/bodies)
    },
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
    """Tune stackweave for the trade-off(s) you care about.  Call ONCE, before run().

    goals: zero or more of "throughput", "latency", "memory", "secure" -- they
        compose, and a higher-precedence goal (secure > memory > latency >
        throughput) wins on any conflicting knob.  No goals = leave the smart
        automatic defaults in place.
    max_fibers: optional hard ceiling on concurrent fibers (backpressure); there
        is no sane automatic value for this, so it stays explicit.

    Returns the dict of EFFECTIVE settings for the knobs it touched (an explicit
    shell env var shows through here, since it wins).
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
    if max_fibers is not None:
        merged["STACKWEAVE_MAX_GOROUTINES"] = str(int(max_fibers))

    # setdefault: an explicit shell env var (or an earlier optimize() call) wins.
    for k, v in merged.items():
        os.environ.setdefault(k, v)

    # STACKWEAVE_STACK_SCRUB is read by the C runtime AT IMPORT (module_init), which
    # is before this post-import optimize() call -- so setting the env var alone
    # would not take effect.  Apply it LIVE through the API: this is what makes
    # optimize("secure") actually scrub recycled stacks (default is off; see
    # stackweave/aio/_base.py).  Mirror an explicit "=0" too (turn scrubbing back off).
    if "STACKWEAVE_STACK_SCRUB" in merged:
        try:
            import stackweave_c
            stackweave_c.set_stack_scrub(os.environ.get("STACKWEAVE_STACK_SCRUB") == "1")
        except (ImportError, AttributeError):
            pass

    # Spawn-path trade, applied LIVE (it picks which C entry stackweave.fiber uses):
    #   throughput -> fiber_fast: max naked-spawn rate, fixed default stack.
    #   memory     -> grow-down : small right-sized resident stacks.
    # Same precedence as the env knobs (memory > throughput), so on a conflict the
    # leaner choice wins.  Untouched unless one of the two is requested, so
    # optimize("latency")/optimize() leave stackweave.fiber at its grow-down default.
    if "throughput" in goals or "memory" in goals:
        want_speed = ("throughput" in goals) and ("memory" not in goals)
        try:
            import stackweave_c
            stackweave_c._fiber_set_speed(1 if want_speed else 0)
        except (ImportError, AttributeError):
            pass

    # Report what is ACTUALLY in effect for those keys (shell overrides show here).
    return {k: os.environ.get(k) for k in merged}

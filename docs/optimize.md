# Tuning: `stackweave.optimize()`

stackweave is correct and fast **with zero configuration** — call nothing and the
runtime tunes itself (best netpoll backend, stall-recovery on free-threaded
builds, calibrating stacks, an auto-sized stack pool, io_uring that engages as
connections climb, …). You should never need to learn a tuning flag.

When you *do* want to lean one way, there is **one function**, and you ask for it
by **the trade-off you're making** — not by memorizing knobs:

```python
import stackweave

stackweave.optimize()                          # auto — the default; nothing to set
stackweave.optimize("throughput")              # max spawn rate
stackweave.optimize("memory")                  # right-sized stacks
stackweave.optimize("latency")                 # sharp tail
stackweave.optimize("secure")                  # hardened
stackweave.optimize("throughput", "latency")   # compose — pass the trades you want
stackweave.optimize("memory", max_fibers=200_000)
```

Call it **before `stackweave.run()`** — some settings are read as the runtime starts.

## The four trades

Each name says what you **buy** and what you **spend** — that's the whole mental
model:

| goal | buys you | spends |
|---|---|---|
| **`"throughput"`** | max spawn rate — `stackweave.fiber` spawns like `fiber_fast` (fixed default stack, no grow-down sampling), and a bigger blocking-offload pool (16 workers) | a little RAM |
| **`"memory"`** | right-sized stacks — `stackweave.fiber` keeps the grow-down auto-sizer (the default; re-enabled if you turned it off) | some spawn rate |
| **`"latency"`** | sharp tail — tighter stall detection (25 ms) so a wedged hub recovers faster | a little CPU (extra watchdog wakeups) |
| **`"secure"`** | hardened — recycled stacks are wiped before reuse (no leftover TLS keys / request bodies) | a little speed |

`max_fibers=N` is the one genuine number with no sane automatic value: a hard
backpressure ceiling on concurrent fibers (the same as
`stackweave.inspect.set_max_fibers(N)`).

These trades are deliberately **safe** — none flips an experimental lever or a
setting that can OOM-kill a RAM-tight host.

> **The stack pool sizes itself.** Out of the box (any preset, or none) the depot
> auto-caps to ~1.5× your live-fiber high-water-mark — clamped by `vm.max_map_count`
> *and* RAM so it can't ENOMEM or balloon — so completions pool instead of churning,
> with no number to set. `STACKWEAVE_STACK_DEPOT_CAP` still forces a static cap if you
> insist. (See [resource-limits](resource-limits.md) for raising `vm.max_map_count`
> past ~30K concurrent fibers on a stock host.)

## Composing

Goals compose — pass several and they merge. On any knob where two goals
disagree, the higher-precedence one wins:

```
secure  >  memory  >  latency  >  throughput
```

The only knob two goals share is the spawn path, so `optimize("throughput",
"memory")` gives you throughput's bigger offload pool *and* memory's right-sized
stacks. It returns the dict of **effective** settings: the env-var knobs it set
(an explicit shell env var shows through, since it overrides optimize()), plus
`"spawn"`, `"stack_scrub"` and `"max_fibers"` for the settings it applied live.

## Power users

`"throughput"` and `"latency"` set two numeric tuning env vars,
`STACKWEAVE_BLOCKPOOL_WORKERS` and `STACKWEAVE_SYSMON_MS` (see
[Resource limits & internals](resource-limits.md)), and only if they are not
already set — so if you export `STACKWEAVE_SYSMON_MS=40` yourself, that sticks.
The rest map onto live APIs: `stackweave.set_grow_down()`,
`stackweave_c.set_stack_scrub()` and `stackweave.inspect.set_max_fibers()`.

## Examples

```python
# RAM-constrained container: just make it lean.
stackweave.optimize("memory")

# Latency-critical RPC tier on a dedicated host, multi-tenant secure.
stackweave.optimize("throughput", "latency", "secure")

# Hard fan-out ceiling on a shared box.
stackweave.optimize(max_fibers=200_000)
```

## Hot handlers: scaling a shared handler across cores

> Full reference: **[Hot handlers](hot-handlers.md)** (`@stackweave.hot`, the
> rules, and why it works). Short version below.

A plain module-level handler already scales across every core — there's nothing
shared for the cores to fight over:

```python
def handle(conn):          # scales flat to as many cores as you have
    ...
```

The one shape that *doesn't* scale on its own is a **shared closure** — a single
handler that *captures* something and is reused for every connection:

```python
config = load_config()

def handle(conn):
    serve(conn, config)    # captures `config`
server.serve(handle)       # the SAME closure runs on every core
```

When many cores run that one closure flat out, they all hammer the same captured
slots and start colliding, so adding cores stops helping. Mark it `@stackweave.hot`
and each core gets its own private copy of the captured slots (pointing at the
same values), so they stop colliding:

```python
@stackweave.hot
def handle(conn):
    serve(conn, config)
```

- It's a **no-op** on a handler that captures nothing (already scales) — safe to
  leave on.
- It costs one copy of the captured slots **per core**, not per fiber (a million
  fibers over one handler still cost one copy per core).
- It stays correct: it only kicks in when the handler *reads* its captures. If it
  *rebinds* one (`nonlocal x; x = ...`), stackweave leaves it shared.
- Stacking decorators? Put `@stackweave.hot` closest to your `def`.

**Fastest path first:** if a handler is hot enough to want this, *compiling* it
(a Cython `cdef` handler) beats it outright — that removes the interpreter cost
entirely, not just the cross-core contention. `@stackweave.hot` is the zero-rewrite
option for when you won't compile.

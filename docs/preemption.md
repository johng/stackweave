# Time-sliced preemption

Under the single-thread scheduler (`run(1)`), stackweave fibers are
**cooperative** by default -- they yield only when
they explicitly call `sched_yield`, sleep, or block on I/O.  If you
write a tight CPU loop with no yield, that fiber monopolises the
scheduler until it returns.

This works for Go-style code (which conventionally has yields
sprinkled through it via channel operations and I/O) but is brittle
when you mix in libraries that don't expect to be cooperative -- a
long `numpy` matmul or a 10-million-iteration arithmetic loop will
starve every other fiber.

`stackweave.preempt_init(10_000)` (the quantum, in µs) solves this on
**free-threaded Python** (the GIL-disabled build).  A timer
thread posts a `Py_AddPendingCall` every quantum; CPython's
`eval_breaker` check -- already done between bytecodes -- invokes our
callback, which calls `runloom_sched_yield()` on the running fiber.

## Hello, preempted fiber

```python
import stackweave

stackweave.preempt_init(10_000)    # quantum_us: 10 ms slices

def hog():
    total = 0
    for i in range(100_000_000):
        total += i * i
    print("hog done", total)

def chatty():
    for i in range(50):
        print("chatty tick", i)
        stackweave.sleep(0.01)

stackweave.fiber(hog)
stackweave.fiber(chatty)
stackweave.run(1)
```

Without `preempt_init`, `chatty` wouldn't get any time until `hog`
finishes.  With it, `chatty` interleaves smoothly because the timer
forces `hog` to yield every 10 ms.

## What's the cost?

The hot path (between yields) pays nothing -- preemption only adds
work when the quantum fires:

- ~300 ns per quantum to dispatch the pending call.
- One stackweave yield (~80 ns asm + snap/load).

At 100 Hz (the default 10 ms quantum), that's ~30 µs of overhead per
real-time second.  ≈ 0.003%.

## How CPython makes this possible

Every bytecode dispatch in CPython's eval loop checks `eval_breaker`
(an atomic flag that signals pending work like signals or pending
calls).  `Py_AddPendingCall` sets the flag; on the very next bytecode
boundary, CPython runs the queued function.

We exploit this by:

1. Starting a timer thread on `preempt_init`.
2. Every `quantum_us` microseconds, the timer thread calls
   `Py_AddPendingCall(yield_cb)`.
3. `yield_cb` checks if any fiber is currently running on this
   thread and, if so, calls `runloom_sched_yield()` to swap it out.

The fiber resumes the next time it's at the head of the ready
queue -- typically immediately after every other ready fiber has
had a slice.

## Caveats

### Bytecode boundaries only

The `eval_breaker` check happens between Python bytecodes.  If a
fiber is sitting inside a long **C call** (e.g. `numpy.dot` on a
huge matrix, `hashlib.sha256` on a multi-MB blob, a blocking system
call), the check doesn't fire -- Python isn't running.  Preemption
will hit as soon as the C call returns.

This is the same limitation Go has with cgo: while you're in C, the
scheduler can't preempt you.  Most stdlib functions release frequently
enough that this isn't noticeable in practice.

### Main-thread scheduler only

`preempt_init` starts one process-wide timer, and `Py_AddPendingCall`
runs its callbacks on the main thread only, so it time-slices the
single-thread scheduler driven from the main thread -- with the GIL off
or re-enabled at runtime (`PYTHON_GIL=1`) -- but not a `run(1)` on
another thread, and never M:N hub threads.  Under the M:N hub model you
don't call it at all: M:N runs are preempted by the sysmon watchdog
instead (see [Combining with M:N](#combining-with-mn)).

## Stopping preemption

```python
stackweave.preempt_fini()
```

Idempotent.  Joins the timer thread.  Use this if you're toggling
preemption on/off for benchmarks -- most production code will just
leave it on after `preempt_init`.

## Choosing a quantum

- **10 ms (10 000 µs)** -- fair scheduling for typical mixed workloads,
  ~0.003% overhead.  This is the default.
- **1 ms (1 000 µs)** -- much finer-grained interleaving, ~0.03%
  overhead.  Use if you've got tight latency requirements (e.g. a
  game-loop-style update with strict frame timing).
- **100 ms** -- coarser, less responsive but lighter on the timer
  thread.  Use if you're CPU-bound and don't have latency-sensitive
  fibers.

```python
stackweave.preempt_init(1_000)     # quantum_us (positional only)
```

## When to use preemption

**Use it when:**

- You have mixed workloads (CPU-bound + I/O-bound) on the same
  scheduler.
- You can't audit every code path for yield points.
- You're running third-party code that might be greedy.

**Skip it when:**

- All your fibers have natural yield points (channels, I/O,
  sleeps) and you're confident none monopolise the CPU.
- You're benchmarking the cooperative baseline and don't want the
  jitter from quantum-driven yields.

The single-thread default is *no preemption*, which matches Go's behaviour
pre-1.14.  Opt into preemption when you actually need it.  (M:N always
preempts -- see below.)

## Combining with M:N

Under M:N (`run(8, ...)`, or `mn_init(8)`) preemption is always on,
with no opt-out, and `preempt_init` is not involved -- its timer does
not preempt fibers on hub threads.  The sysmon watchdog thread preempts
any fiber that has been running Python on its hub for longer than the
time slice, `STACKWEAVE_PREEMPT_MS` (default: the 50 ms sysmon wedge
budget, `STACKWEAVE_SYSMON_MS`).  Each hub's currently-running fiber gets
preempted independently.  Two CPU-bound fibers on different hubs
will both make progress without needing to yield to each other
(they're on different OS threads); preemption keeps any single hub
from being monopolised by one greedy fiber.  A preempted fiber resumes
on the hub it was preempted on: its slice can end anywhere, including
inside `with rlock:` or an import, whose locks belong to the OS thread.
A fiber that yields or sleeps can resume on another hub.

See [Parallelism](parallelism.md) for the M:N model.

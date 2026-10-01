# API reference

Every public symbol exported by stackweave, organised by module.  This is
a reference -- start with the [guides](index.md) if you're learning
your way around.

## `stackweave_c` (C extension)

The low-level scheduler API.  Most user code calls `stackweave.fiber` and
`stackweave.run`; everything else is for advanced use.

### Scheduler control

#### `fiber(fn, stack_size=0) → G`

Spawn a fiber running `fn`.  Returns a [`G`](#g) handle.

- `fn` -- a zero-arg callable.  Bind arguments with `lambda` or
  `functools.partial`.
- `stack_size` -- optional per-call override (bytes; 0 = default).  Bypasses the
  scheduler's calibrated default.  See [stack sizing](stack-sizing.md).

#### `fiber_noyield(fn) → G`

Like `fiber(fn)` but with a contract: `fn` promises not to yield, sleep,
park, or do monkey-patched I/O.  The scheduler skips per-g datastack
setup, saving 150–400 ns per spawn.  **Undefined behaviour if `fn`
yields.**  Use only for pure-compute callables.

#### `run() → int`

Drive the scheduler until every fiber has finished.  Returns the
count of completed fibers.

#### `sched_yield()` / `sched_yield_classic()`

Cooperatively yield.  `sched_yield` is a vectorcall fastpath
singleton; `sched_yield_classic` is the equivalent PyCFunction (kept
for benchmarking, otherwise identical).

#### `sched_sleep(seconds)`

Park the current fiber until at least `seconds` have elapsed.
Other fibers run in the meantime.

#### `sched_stop()`

Signal the scheduler to exit its drain loop at the next safe point.
Used internally by `stackweave.aio` for early termination.

#### `sched_reset() → (int, int, int)`

Drop everything queued in the scheduler (ready FIFO, sleep heap,
netpoll-parked).  Returns `(n_ready, n_sleep, n_parked)`.  Used by
`stackweave.aio.run` for cleanup between runs.

#### `park_self()`

Park the current fiber until `g.wake()` is called on its handle.
Race-safe -- a wake that arrives before the park is consumed and the
park returns immediately.  Use with `current_g()` to capture the
handle before parking.

#### `current_g() → G | None`

Return a handle to the currently-running fiber, or `None` if
called from outside any fiber.

### Stack sizing

#### `get_stack_size() → int`

Current per-fiber default stack size, in bytes.

#### `set_stack_size(bytes)`

Override the default and freeze calibration.  Clamped to
`[256 KB, 8 MB]`.  Disables stack painting.

#### `current_g_hwm() → int`

The currently-running fiber's stack high-water-mark in bytes
(page-granular, paint-free via `mincore`), or `0` outside a fiber.  The
read half of the function-bound grow-down auto-sizer.

#### `set_grow_down(enabled=True)` / `grow_down_enabled() → bool`

(On `stackweave`, not `stackweave_c`.) Toggle the function-bound stack **grow-down**
auto-sizer, which learns each `stackweave.fiber()`-spawned function's real stack need
and reserves only that.  **On by default**, active under M:N (`run(n>1)`) only;
single-thread `run(1)` keeps the fixed default.  A per-call `stackweave.fiber(fn,
stack_size=N)` pin always wins, and grow-down defers to the opt-in
`enable_stack_autosize()` when that is explicitly enabled.  See
[docs/stack-sizing.md](stack-sizing.md#automatic-grow-down-on-by-default-mn).

### Channels

#### `Chan(capacity)`

Construct a channel of the given buffer capacity.  `Chan(0)` is
unbuffered (rendezvous).  See [Channels](channels.md).

Methods:

- `send(value)` -- block until the value fits, then enqueue.  Raises
  `ValueError` on closed channel.
- `recv() → (value, ok)` -- block for a value.  Returns
  `(None, False)` after the channel is closed and drained.
- `try_send(value) → bool` -- non-blocking send; `False` if the buffer
  is full.
- `try_recv() → (value, ok) | None` -- non-blocking; `None` if buffer
  empty.
- `close()` -- wake every parked sender (they raise) and receiver
  (they get `(None, False)`).
- `__iter__()` -- yields values until the channel closes.

#### `select(cases, default=False)`

Multi-way wait.  Each case is `("recv", chan)` or `("send", chan, value)`.

- Returns `(idx, payload)` for a fired case where `payload` is
  `(value, ok)` for recv or `None` for send.
- Returns `-1` (bare integer) if `default=True` and no case is ready.

### Networking primitives

#### `wait_fd(fd, events, timeout_ms=-1) → int`

Park the current fiber until `fd` is ready.  `events` is a
bitmask: `1 = read`, `2 = write`.  Returns the ready bitmask.
`timeout_ms=-1` for no timeout; 0 to poll without parking.

#### `fd_read(fd, buffer, n) → int`, `fd_write(fd, data) → int`

Cooperative read (into a writable buffer, returns bytes read) / write on
an fd.  Park on `wait_fd` when EAGAIN.

#### `tcp_recv(fd, buffer, n, flags=0) → int`, `tcp_send(fd, data, flags=0) → int`

TCP-specific fastpaths on a socket's `fileno()`.  `tcp_recv` receives into
a writable buffer and returns the byte count; `tcp_send` loops until all
of `data` is sent.

#### `file_read(fd, buffer, n, offset=-1) → int`, `file_write(fd, data, offset=-1) → int`

File I/O.  On Linux 5.1+ with `iouring_available()`, dispatched
through io_uring.  Elsewhere a plain (blocking) `read`/`pread` or
`write`/`pwrite`.

#### `iouring_available() → bool`

True if the kernel supports io_uring (Linux 5.1+).

### M:N parallelism

See [Parallelism](parallelism.md).

- `mn_init(n=0)` -- start `n` hub threads (defaults to `cpu_count`).
- `mn_fiber(fn, stack_size=0, hub=-1) → None` -- spawn on a round-robin hub
  (`hub=N` pins it to hub N).  Returns no handle.
- `mn_run() → int` -- wait for all hubs to drain.
- `mn_fini()` -- tear down the pool.

### Preemption

See [Preemption](preemption.md).

- `preempt_init(quantum_us=10000, /)` -- start the process-wide quantum timer (time-slices the main thread's single-thread scheduler; M:N runs are preempted by sysmon).
- `preempt_fini()` -- stop the timer.

### Pre-warming

#### `warmup(n, stack_size=131072, /)`

Pre-allocate `n` fiber stacks so the first `n` spawns skip mmap.

### Diagnostics

#### `backend() → str`

Active context-switch backend: `"fcontext-asm"` or `"ucontext"`.

#### `netpoll_backend() → str`

Active netpoll: `"epoll"`, `"kqueue"`, or `"select"`.

#### `stats() → dict`

Snapshot of scheduler counters.  Keys: `ready`, `sleeping`,
`netpoll_parked`, `completed`, `running`, `stack_size_default`,
`stack_hwm`, `stack_completed`, `stack_calibrated`, `stack_painting`,
`backend`, `netpoll`.  Cheap; safe to poll periodically.

### Goroutine introspection

A Go-style fiber dump -- which fibers exist, what each is blocked
on, and where in your code.  See the [Debugging guide](debugging.md) for
the full picture (and the friendlier `stackweave.inspect` wrappers).

#### `fibers() → list[dict]`

One dict per live fiber: `id`, `state` (`running` / `runnable` /
`io-wait` / `sleep` / `chan-wait` / `park` / ...), `blocked_on`, `fd` +
`events` (when `io-wait`), `wake_in` (when `sleep`), `age`, `refcount`,
`noyield`, `owner`.  Cheap; safe from a watchdog.

#### `fiber_count() → int`

Number of live fibers.

#### `mn_hub_states() → list[dict]`

One dict per M:N hub — the per-**hub** view: `id`, `state` (`detached` /
`attached` / `suspended`), `running_g` (goid being resumed, or `None`),
`dwell_ms` (how long that resume has run), `pending`, and `blocked_at`
(best-effort Python call site of a
DETACHED-wedged hub's blocking call).  Lock-free atomic reads; `[]` when the M:N
scheduler isn't running.  The friendly wrapper `stackweave.inspect.hubs()` adds a
`stack_cmd` (`py-spy dump --pid <PID>`) per row, and `stackweave.inspect.print_hubs()`
renders the table — see [debugging.md](debugging.md#what-is-each-hub-doing-hubs).

#### `fiber_stack(id) → (callable_repr, [(file, line, func), ...])`

Best-effort reconstructed Python stack of one fiber (deepest first).
Full stack under the single-thread scheduler (`stackweave.aio`) and under
M:N, where every fiber owns a thread-state that is claimed for the walk.

#### `dump_fibers(fd=2) → None`

Write an async-signal-safe structural dump (state histogram + per-fiber
line, no Python objects) to `fd`.  The SIGQUIT path -- usable from a signal
handler and when the interpreter is wedged.

#### `set_introspect_timestamps(bool)`

Track each fiber's park time so `fibers()`/dumps report `age`.  Off
by default (one clock read per park).

#### `install_traceback_signal(signum=SIGQUIT) → int`

Install a raw-C signal handler that dumps all fibers to stderr -- Go's
`GOTRACEBACK` / `kill -QUIT`.  POSIX only.

#### `reset_after_fork() → None`

Reset the runtime to a clean single-process state in a forked child
(abandons the dead hub/offload threads, re-inits inherited locks, gives the
child its own netpoll fd).  Registered automatically as an
`os.register_at_fork(after_in_child=...)` handler; see the [Debugging
guide](debugging.md#fork-safety).

#### `set_deadlock_mode(0|1|2)` / `get_deadlock_mode() → int` / `count_deadlocked() → int`

Deadlock detection: when the single-thread scheduler quiesces with
fibers still blocked on channels/parks, mode 0=off, 1=warn (print the
dump, default), 2=raise `RuntimeError`.
`count_deadlocked()` is the current chan/park-blocked count.

#### `set_max_fibers(n)` / `get_max_fibers() → int` / `live_fibers() → int`

Backpressure: cap the live-fiber count (0 = unlimited).  Over the cap,
spawn raises `RuntimeError`.  Also `STACKWEAVE_MAX_GOROUTINES`.  Zero hot-path cost
when unset.

#### `set_introspect_timestamps(bool)` / `get_introspect_timestamps() → bool`

Park-age tracking (enables the `age` field + `stackweave.inspect.leaked()`).

### Crash reporting & stack tuning

Exposed as friendlier wrappers on `stackweave.inspect` (also as raw `stackweave_c`
functions).  See the [Debugging guide](debugging.md#crash-reporting-sigsegv-sigbus)
and [Stack sizing](stack-sizing.md).

#### `inspect.install_crash_handler(level=None, file=None)` / `uninstall_crash_handler()` / `crash_handler_installed() → bool`

Install a fatal-signal handler (SIGSEGV/SIGBUS/...) that, on a crash, classifies
the fault against the per-fiber guard pages -- a fiber stack overflow is
named and distinguished from a wild pointer -- and dumps the live-fiber
registry, then chains to the default handler.  `level`:
`on`/`all`/`backtrace`/`pystack`/`wait`/`gdb`/`off` (default: the fiber dump).
`file` also appends the report there.

#### `inspect.start_watchdog(secs)`

Start the self-hang watchdog: a detached native thread that writes a hang report
(build + runtime snapshot, flight recorder; no abort) when no fiber has
completed for `secs` seconds while work is outstanding.  The fiber dump and the
report-file copy follow `install_crash_handler()`'s level and `file`, so call
that first.  Idempotent; `ValueError` if `secs <= 0`.  POSIX only.  See
[Reporting crashes](reporting-crashes.md#the-self-hang-watchdog-start_watchdogsecs).

#### `inspect.enable_stack_advice(on=True)` / `stack_advice() → list[dict]` / `print_stack_advice(file=None)`

Per-fiber-kind stack profiler.  While on, each kind's real C-stack
high-water mark is measured; `stack_advice()` returns `{kind, samples, max_hwm,
reserved, suggested}` per kind so you can right-size `stack_size=`.  Advisory
only -- it never changes a stack size.  Off by default (zero cost).

#### `inspect.enable_stack_autosize(on=True, prescan=False)`

Adaptive auto-sizer: each fiber kind starts large and, once measured, its
later fibers start at the learned size ("start large, learn down").
In-memory only -- never persisted.  `prescan=True` also runs the cold-start
optimizer (a deep-frame kind like `Decimal` starts big enough to survive its
first run).  An explicit `stack_size=` always wins.

### Thread setup

#### `thread_init()` / `thread_fini()`

Per-OS-thread setup/teardown.  Called automatically; only invoke
manually if you're embedding stackweave in a non-main thread.

### Types

#### `G`

Goroutine handle.  Attributes:

- `done` -- `True` once the fiber has returned.
- `result` -- return value (or `None` until done).
- `exception` -- exception object if the fiber raised, else `None`.
- `wake()` -- re-queue a parked fiber; race-safe with
  `park_self()`.
- `stack()` -- a small state dict, `{'state': ..., 'has_snap': bool}`;
  for the Python frames use `fiber_stack(id)`.

#### `Coro`

Lower-level coroutine handle.  Most users won't construct these
directly; `G` wraps a `Coro` plus scheduler metadata.

---

## `stackweave`

Top-level package.  Re-exports a Go-style API from `stackweave.runtime`
(the original Python-only scheduler, kept for backward compatibility).

```python
import stackweave

stackweave.fiber(fn)            # spawn (uses the C scheduler under the hood)
stackweave.yield_now()       # cooperative yield (give other fibers a turn)
stackweave.sleep(seconds)    # cooperative sleep
stackweave.run(n, main_fn=None)  # THE entry point. run main_fn with n hubs:
                          #   n=1 single-thread, n>1 M:N parallel across n
                          #   cores (needs the GIL off; n>1 under the GIL
                          #   raises).  main_fn optional -> drain-only.
                          #   Collapses mn_init/mn_fiber/mn_run/mn_fini.
stackweave.current() → Goroutine
stackweave.backend() → str
```

For new code, prefer `stackweave_c` (faster) or `stackweave.sync` (richer API).

---

## `stackweave.inspect`

Runtime introspection -- the friendly wrappers over the fiber
registry.  `fibers(stacks=)`, `count()`, `stack(id)`, `format(stacks=)`
(a human dump as a string), `dump(file=, stacks=)`, `enable_timestamps()`,
`install_dump_signal()`, `leaked(min_age, states)` / `watch_leaks(...)`
(leak detection), `set_deadlock_mode("off"/"warn"/"raise")`,
`set_max_fibers(n)` / `live_fibers()` (backpressure).  See the
[Debugging guide](debugging.md).

```python
import stackweave.inspect as gi
print(gi.format(stacks=True))   # which fibers, and where they're stuck
gi.install_dump_signal()        # kill -QUIT <pid> -> dump
```

---

## `stackweave.aio`

Asyncio bridge.  See [stackweave.aio](asyncio.md).

```python
stackweave.aio.run(coro)                     # equivalent of asyncio.run
stackweave.aio.install()                     # set StackweaveEventLoopPolicy globally
stackweave.aio.open_connection(host, port)   # async (reader, writer)
stackweave.aio.start_server(cb, host, port)  # async server: .close(), .sockets
```

Classes:

- `StackweaveEventLoop` -- drop-in `asyncio.AbstractEventLoop` backed by
  stackweave's scheduler.
- `StackweaveEventLoopPolicy` -- sets `StackweaveEventLoop` as the default loop.
- `StackweaveFuture` -- duck-typed Future with synchronous done-callback
  dispatch.
- `StackweaveTask` -- `asyncio.Task` replacement that drives the coroutine
  inside a fiber.
- `StreamReader` / `StreamWriter` -- asyncio-compatible stream
  interface, backed by `wait_fd`.
- `DatagramTransport` -- UDP transport for
  `loop.create_datagram_endpoint`.

---

## `stackweave.sync`

No-`async`/`await` facade.  See [Sync API](sync-api.md).

```python
stackweave.sync.fiber(fn, *args, **kwargs)        # spawn with args
stackweave.sync.run(main_fn=None)              # drive scheduler
stackweave.sync.sleep(seconds)
stackweave.sync.yield_now()
stackweave.sync.current() → G

stackweave.sync.Chan                            # re-export of stackweave.Chan
stackweave.sync.select                          # re-export of stackweave.select
stackweave.sync.park / wake                     # park_self + wake helpers

stackweave.sync.tcp_connect(host, port) → Socket
stackweave.sync.tcp_listen(host, port, *, backlog=128) → Socket
stackweave.sync.udp_endpoint(local_addr=None, remote_addr=None) → Socket
```

Synchronisation primitives matching `asyncio.*`:

- `stackweave.sync.Lock` -- cooperative mutex.
- `stackweave.sync.Event` -- set/clear/wait.
- `stackweave.sync.Condition` -- waiter + notifier on a lock.
- `stackweave.sync.Semaphore` -- bounded counting semaphore.

#### `Socket`

Wrapper around `socket.socket` whose blocking methods (`connect`,
`accept`, `recv`, `send`, `sendall`, `recv_into`, `recvfrom`, `sendto`)
park cooperatively on `wait_fd`.  Standard `socket.socket` attributes
(`setsockopt`, `fileno`, `getsockname`, `close`, etc.) pass through.

---

## `stackweave.time`

Go-style timers and tickers.

#### `Sleep(seconds)`

Cooperative sleep.  Same as `stackweave.sleep`.

#### `After(seconds) → Chan`

Returns a 1-buffered channel that receives `seconds` once, after `seconds`
seconds, then closes.  Equivalent of Go's `time.After`.

```python
import stackweave
import stackweave.time as t

def main():
    after = t.After(1.0)
    # ... do work ...
    after.recv()           # blocks until the timer fires

stackweave.run(1, main)
```

#### `NewTimer(seconds) → Timer`

Single-shot timer.  Methods:

- `Timer.C` -- channel that fires once.
- `Timer.Stop()` -- cancel; returns `True` if cancelled before firing.
- `Timer.Reset(seconds)` -- rearm.

#### `NewTicker(seconds) → Ticker`

Recurring ticker.  Methods:

- `Ticker.C` -- channel that fires every `seconds`.
- `Ticker.Stop()` -- stop emitting.

#### `Tick(seconds) → Chan`

Shorthand for `NewTicker(seconds).C` when you don't need to stop it
(the ticker leaks -- only use for program-lifetime tickers).

---

## `stackweave.monkey`

Stdlib monkey-patching.  See [Monkey-patching](monkey-patching.md).

#### `patch(**flags)`

Apply patches.  Default: all categories enabled.  Opt out:

```python
stackweave.monkey.patch(threading=False, dns=False)
```

Categories: `socket`, `time`, `os`, `select`, `stdio`, `ssl`,
`subprocess`, `threading`, `queue`, `dns`.

#### `unpatch(**flags)`

Reverse patches.  Without args, reverses everything applied.

#### Co-aware synchronisation primitives

Available even without `patch()`:

- `CoLock`, `CoRLock` -- cooperative mutexes.
- `CoEvent` -- set/wait.
- `CoCondition` -- `wait` releases the lock cooperatively.
- `CoSemaphore`, `CoBoundedSemaphore` -- counting semaphores.

These implement the `threading.*` interface but park fibers
instead of OS threads.  Useful when you want sync primitives but
don't want to install the full monkey patch.

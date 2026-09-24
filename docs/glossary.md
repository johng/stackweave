# stackweave glossary

For engineers reading the codebase for the first time. Terms are grouped by
subsystem rather than alphabetised, because most of them only make sense
next to their neighbours.

Anything marked *(inferred)* is read off code and gate output rather than
from a design doc — treat those as good-faith reconstructions.

---

## The execution model

**fiber** (also **goroutine**, **g**) — a stackful coroutine with its own C
stack. Suspends and resumes at arbitrary depth, unlike an `async def` which
can only yield at an `await`. This is why plain synchronous libraries work
under monkey-patching: nothing needs rewriting. The C struct is `runloom_g_t`.

**hub** — an OS thread running a scheduler loop, with its own run queue,
parker pool and (usually) poller. Go's `P` and `M` fused into one object.
`stackweave.run(8, main)` starts 8 of them.

**M:N** — M fibers multiplexed across N OS threads. Only meaningful with the
GIL off, since otherwise the hubs cannot run Python simultaneously.

**free-threaded** / **3.14t** / **GIL-off** — a CPython build (`PYTHON_GIL=0`)
with no global interpreter lock. The `t` suffix marks it. Real parallelism,
and also the reason a bare `counter += 1` across fibers loses updates.

**tstate** (`PyThreadState`) — CPython's per-thread interpreter state. Central
to most of stackweave's hard problems, because a suspended fiber's frames bake in
the tstate pointer of the hub they were suspended on.

**coro** — the low-level stack-switching primitive under a fiber
(`coro.c`; assembly or ucontext).

---

## Scheduling

**run queue / deque** — each hub's local queue of runnable fibers. A
**Chase-Lev work-stealing deque** (`cldeque.c`): the owner pushes and pops one
end lock-free, thieves claim the other end. Crucially, a thief needs **no
cooperation from the owner**, so a blocked hub's queued work is still
stealable.

**work stealing** — an idle hub takes work from a busy one's deque. stackweave
uses Go's **randomOrder**: each hub starts at its own id and steps by a stride
coprime with the hub count, so every victim is visited exactly once and K idle
hubs don't all hammer hub 0's CAS in lockstep.

**global runq** — a process-wide queue that any hub drains. Every M:N run
routes *woken* fibers here, so a fiber woken while its origin hub is blocked
resumes on another hub instead of stranding.

**submission list** (`runloom_mn_hub_submit`) — a hub's owner-drained inbox.
Nothing else drains it, so work that lands here strands if its hub blocks;
woken fibers bypass it for the global runq.

**park / unpark** — a fiber suspending until some event (fd readiness, a
channel, a timer), and being made runnable again. `wake_g` is the wake path.

**pending / completed** — per-hub atomic counters of in-flight and finished
fibers, summed by `mn_run` to decide when a run is over. Sharded per hub
because a single global counter was a contended cache line.

**spawn pace** (`STACKWEAVE_SPAWN_PACE`) — a cooperative yield every N spawns, so
a fiber spawning in a tight loop interleaves running with spawning instead of
monopolising its hub.

---

## sysmon and preemption

**sysmon** — a dedicated watchdog thread (Go's namesake). Holds no GIL and no
tstate; reads per-hub atomics and writes to stderr. Detects **wedges** and
drives **preemption**. Deep-sleeps when no hub is running work.

**wedge** — a hub whose current fiber resume has exceeded
`runloom_sysmon_wedge_ns` (50 ms default) without yielding. Reported once per
episode with a matching `RECOVERED` line. Logging is off unless
`STACKWEAVE_SYSMON=1`.

**ATTACHED / DETACHED / SUSPENDED** — how sysmon classifies a wedge, by the
hub tstate's attach state:
- *ATTACHED* — still holds its tstate: CPU-bound Python, or a raw syscall that
  never released it. **Preemptable, and preempted.**
- *DETACHED* — released the tstate, i.e. genuine blocking I/O. Left alone;
  work-stealing already drains its fresh fibers.
- *SUSPENDED* — parked by a stop-the-world (GC).

**preemption** — the explicit time-slicer, `preempt_init(quantum_us)`, posts a
pending call every quantum that yields the running fiber. The M:N scheduler
has no wall-clock preemption: migration mode stands it down, so a fiber that
never yields keeps its hub until it does.

**strand** — work that can never run because the only thing that would schedule
it is itself blocked. The failure mode this codebase worries about most.

**lost wakeup** — a wake that was signalled but never delivered, leaving a
fiber parked forever. Distinguished from a strand: the wake *happened*.

---

## Netpoll (I/O readiness)

**netpoll** — the readiness layer: epoll on Linux, kqueue on BSD/macOS, select
elsewhere. Fibers park on an fd and are woken when it is ready.

**parker** — the per-park record linking an fd to the fiber waiting on it.
Lives in a **parker pool**, one per hub (up to
`RUNLOOM_PARKER_POOL_HUBS`, 64), so registration and wake don't serialise
across hubs on one kernel lock.

**per-hub epoll** — each hub polls its own epoll set plus a wake eventfd,
instead of all hubs sharing one `runloom_epoll_fd`. Measured **+34–40 %**
saturation throughput on a 64-core box versus the old shared set.

**arm / disarm** — adding or removing an fd's interest bits in the poller.
Level-triggered, so a stale arm with no waiter makes `epoll_wait` return
instantly forever — the 2026-08 busy-spin bug, fixed by adding
`runloom_netpoll_disarm_in` to mirror the existing `disarm_out`.

**self-pipe / wake eventfd** — an fd whose only job is to be written to, so a
hub blocked in `epoll_wait` can be interrupted.

**io_uring** — Linux's async-completion interface; used for real async file
I/O where available, versus dispatching to a worker thread.

---

## Blocking work

**blocking call** — something stackweave cannot make cooperative: buffered file
`read`/`write`, a C-extension DB driver, `socket.gethostbyaddr` (libc, and it
takes no timeout), CPU-bound hashing. It must not run on a general hub, or that
hub's scheduler loop stops.

**offload** (`stackweave.monkey.offload`) — the sanctioned escape hatch for
running one.

**backend thread pool** (`_ThreadPoolBackend`) — the original offload
mechanism: bare OS threads blocking in a raw `SimpleQueue.get()`, outside the
scheduler entirely. Because those workers are not fibers, submission,
completion and wakeup are all hand-rolled — which is where that subsystem's
bugs have historically lived.

**FD-mode / inmem parker** — the pool's two completion-wake strategies. FD-mode
gives each task a self-pipe (carries signal-interrupts into the call, ~10
syscalls); the inmem parker uses none (faster, but off the netpoll). Chosen
adaptively by queue backlog.

**offload hub** — a hub reserved to run blocking calls as *ordinary fibers*
(`offload_hubs=K`). Excluded from general placement and work-stealing (both
directions), so no general work can land on one and stall. The offload fiber is
spawned on its hub and the caller parks on a normal channel.

---

## Memory, GC and migration

**snap** — the saved interpreter state of a suspended fiber (frame chain, stack
refs). The seam between a fiber's Python state and CPython's tstate.

**frames anchor** (`module_gcframes.c.inc`) — one GC-tracked object whose
traverse walks every parked fiber's frame chain. Without it the free-threaded
collector cannot see frames living in `g->snap` and frees their referents early
→ use-after-free on resume.

**greg** — the fiber registry the anchor walks. Any spawn path that bypasses
`runloom_greg_link` reopens that GC blind spot.

**TLBC** — CPython's thread-local bytecode. Interacts badly with stackful
fibers; kept on only when the frames anchor is active.

**migration** — resuming a suspended fiber on a *different* hub. Always on
under M:N: every fiber owns its own tstate, so a woken fiber resumes on any
idle hub. Unsound on stock CPython for two independent reasons, either of which
corrupts:
- *allocation* — the fiber allocates on the origin hub's mimalloc heap
  (`heap->thread_id` mismatch → `_mi_page_retire` corruption). Fixed by
  `Py_TSTATE_ALLOC_HOME`.
- *execution* — the compiler may hoist the reads identifying the running OS
  thread, so a resumed fiber keeps using the origin hub's tstate. Fixed by
  `Py_TSTATE_EXEC_HOME`.

Both patches (`src/patches/`) are required for a sound M:N run; nothing checks
for them at runtime.

**slab** — the allocator for `runloom_g` structs. A freed g is **retained**,
never returned to the OS: a stale dup-wake still dereferences it, so freeing
would turn a benign race into a SIGSEGV.

---

## Testing and verification

**check_all_fast.sh / check_all_extensive.sh** — the local merge gates (there is
no hosted CI, deliberately). Fast runs before any merge; extensive before a
risky one.

**run_isolated.py** — runs one test file per subprocess. In-process `pytest
tests/` flakes on cross-file state leaks.

**big_100** — a corpus of large adversarial scenario programs (`pNNN_*.py`),
each stressing one mechanism with an explicit oracle. Referenced by number in
commit messages, e.g. p92, p224.

**bughunt_repros** — minimal standalone reproductions, exit 1 when the bug is
present. Kept after the fix as regression oracles.

**soak** — long-running load with slope-based failure detection ("fail on a
slope, not a crash"), so gradual leaks surface.

**MR3 / metamorphic relation** *(inferred)* — run the same program at different
hub counts (e.g. 2 and 8) and assert the observable result is identical. Catches
scheduler bugs that change *results* rather than crashing.

**DST / mn-sim** — deterministic simulation testing: a seeded, controlled
scheduler that replays an exact interleaving. Work-stealing and wall-clock
ordering are disabled while armed, since either would make replay
non-deterministic.

**lincheck** *(inferred from the phase name)* — linearizability checking of the
concurrent data structures.

**TLA+ / TLC** — model checking of scheduler protocols. Includes *negative
controls*: checks expected to FAIL, so a control that starts passing means the
model stopped detecting its injected bug.

**CBMC / GenMC** — bounded model checking and stateless model checking of C,
used on the deque and the refcount protocol.

**TSan** — ThreadSanitizer. "ext-only" instruments just the extension and
cannot attribute anything crossing into CPython; "gold" is a fully instrumented
interpreter and is the authoritative verdict.

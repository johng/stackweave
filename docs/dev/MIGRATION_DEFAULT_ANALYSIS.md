# Migration mode as the default: where the cost moves

Status: analysis, 2026-09-15. Written against `fibre-hubs` (de94a1e5).
Update 2026-09-25: migration is now the ONLY M:N mode (PR #23), so the opt-in
wording below is historical; sections 0b, 1, 2 and the sleeper gap in 4 are
still open and tracked in that PR's review.
Nothing here is implemented unless a later section says so.

Migration mode (`STACKWEAVE_MIGRATION=1` / `stackweave.enable_migration()`) gives
every fiber its own `PyThreadState`, so a woken fiber can resume on any hub.
Today it is opt-in and gated on the two CPython patches (alloc-home and
exec-home). This note asks: if it became the default, what would we change?

The short answer: the queues stop being the hot path and the thread-state
handling becomes it. Three costs dominate, one correctness gap must close
first, and several structures become simpler or redundant.

## 0. Close first: offload hubs can pull from the global run queue

The neighbour-steal block in `mn_sched_hub_main.c.inc` is fenced against
offload hubs in both directions (exclusion 2 of the `RUNLOOM_OFFLOAD_HUBS`
design). The global run-queue pull just above it is not: it runs for every
hub, offload hubs included.

In default mode this is harmless because the global queue is always empty.
With migration on, an offload hub between blocking calls pulls any unpinned
woken fiber. If that fiber then yields cooperatively or is preempted it lands
on the offload hub's local ready ring and strands behind the next blocking
call, which is exactly the strand the four exclusions exist to prevent.

The pull needs a guard so that an offload hub only takes entries pinned to
itself. This is a prerequisite, not an optimisation.

## 0b. Close first: the deadlock census is inert under migration

Found while regression-testing the local-wake change (2026-09-15):
`test_swarm_mn_sched.py::test_mn_deadlock_raises_under_mn_run` hangs forever
in migration mode on the unmodified `fibre-hubs` tree as well. Cause chain:

- `mn_sched_sysmon.c.inc` forces `runloom_preempt_enabled = 0` under
  per-g-tstate mode (the eval-frame preempt wrapper is not wired for it).
- `runloom_sysmon_enabled` is set from `RUNLOOM_PREEMPT` unless
  `RUNLOOM_SYSMON` is given explicitly, so it goes off with preemption.
- `runloom_mn_has_wakeable_work` returns "wakeable" whenever sysmon is off
  (it cannot distinguish a running fiber from a deadlocked one without the
  resume instrumentation), so `mn_run` never declares a deadlock.

So with migration as the default, a genuine deadlock hangs instead of
raising, and CPU-bound fibers are never preempted. Either the preempt
wrapper must be made per-g-tstate-safe, or the sysmon resume instrumentation
must be enabled independently of preemption in this mode. Until then, run
migration-mode suites with `RUNLOOM_SYSMON=1` and deselect the deadlock
tests.

## 1. Four attach transitions per resume

The per-g-tstate resume path does:

    PyEval_SaveThread()            detach hub tstate
    PyEval_RestoreThread(g->tstate) attach fiber tstate
    runloom_coro_resume(g->coro)
    PyEval_SaveThread()            detach fiber tstate
    PyEval_RestoreThread(hub_ts)   reattach hub tstate

On free-threaded CPython each transition is the full attach protocol with
stop-the-world checks. Default mode does the snap dance instead, which is a
handful of register-width copies.

The hub only needs its own tstate attached when the scheduler itself runs
Python (completion decref, timer callbacks, the occasional diagnostic). So:

- Two transitions per switch: leave the previous fiber's tstate attached
  until the hub needs its own, and go fiber-to-fiber directly.
- The scheduler-side Python that does need a tstate can attach the hub's
  lazily at that site.

Measure first: a park/wake ping-pong microbench in both modes. The
expectation is that this dominates the migration-mode switch cost.

## 2. A full thread state per fiber, allocated and deleted every time

Spawn under migration calls `PyThreadState_New`, which takes the runtime
lock and links into the interpreter thread list. Completion runs
`PyThreadState_Clear` + `PyThreadState_Delete`. Nothing is pooled.

Two consequences:

- Every stop-the-world pause walks the interpreter thread list. One million
  live fibers means one million entries per GC pause.
- A free-threaded tstate carries its own mimalloc heaps and per-thread
  refcount structures. That collides with the millions-of-fibers story.

Fix: a per-hub pool that recycles tstates on completion instead of
deleting them. The alloc-home patch makes this cleaner than it sounds: a
pooled tstate's own heap stays empty by construction, because every
allocation is redirected to the running hub's heap.

Related and cheap: migration mode allocates the coro and guarded stack
eagerly at spawn because "the global-runq resume path assumes g->coro is set
at claim time". But a fresh fiber never arrives through the global queue;
only parked fibers do, and they already have a coro. The deferral that
default mode uses to avoid the spawn-burst mmap storm can be restored
unchanged.

## 3. One mutex in front of every cross-hub wake

The global run queue is a linked list under a single process-wide mutex,
with a first-match walk so pinned entries do not block unpinned ones behind
them. As a rescue path that was fine. As the primary wake path it is a
contended lock on every wake and every idle pull.

Two steps, in order:

- **Split pinned entries out.** A per-hub pinned count already exists
  (`runq_pinned_n`), so the natural shape is one per-hub pinned queue plus
  one global unpinned queue, both O(1). The walk disappears.
- **Go-style local wake.** A wake performed by a hub thread pushes the fiber
  onto the waker's own Chase-Lev deque, which is a legal owner push with no
  lock. The global queue then serves only foreign-thread wakers (main-thread
  signals, asyncio bridge threads, mp feeders) and pinned entries. The
  PARKED->QUEUED transition already happens before the push, so the wake
  state machine is unchanged; only the destination moves. This also gives
  cache locality: the waker just touched the data the fiber will read.

  **Implemented on this branch** (`runloom_mn_woken_enqueue` in
  `mn_sched_mn_api.c.inc`; guard: `tests/test_local_wake.py`). The deque
  now carries QUEUED fibers as well as fresh ones under migration, so
  hub_main flags a QUEUED g at the pick step and runs the same claim and
  queue-ref drop it runs for a global pull; the park-side surplus scan
  counts a deque of one under migration so a busy waker's deque is never
  slept through. Measurements: see the commit message.

## 4. Structural simplifications that follow

- **Yielders should not pin themselves.** `runloom_mn_yield_current` and the
  preempt path push to the local ready ring, so in migration mode a yielded
  fiber is hub-bound for no reason. Push to the owner's deque instead and
  the whole runnable population becomes stealable. Add one Go-style
  `runnext` slot for the most recently readied fiber to keep the
  hot-before-fresh ordering; then the ready ring, the starvation bound, and
  the ready-streak logic can all go.
- **Steal half, not one.** A thief takes one item per attempt. With bulk
  spawns filling 4096-deep deques an idle hub steals one, runs it, and comes
  back. Chase-Lev supports a batch steal by advancing `top` by k in one CAS.
- **The world-yield courtesy pause** exists because pinned work strands on a
  suspended hub. Once nothing is pinned except by explicit request it
  narrows to the pinned count, or goes away.
- **Sleepers remain a residual gap.** The sleep heap is per-hub and
  owner-fired, so a wedged hub still strands its sleepers even with
  migration. A global timer wheel owned by the pump thread, firing into the
  global queue, closes it. Lower priority: liveness, not throughput.
- **The GC frames anchor** may be redundant here, because a parked fiber's
  frames now live on a real tstate the collector already walks. That anchor
  is memory-safety load-bearing (see CLAUDE.md), so do not touch it without
  `tests/test_tlbc_parked_frame_gc.py` as the oracle.
- **Offload hubs** lose their "nothing migrates" rationale but keep their
  purpose. They reduce to hubs excluded from placement and steal, with
  offload fibers as pinned fibers, which is roughly the shape the
  `fibre-hubs` pinning work already builds toward.

## Measurement order

1. Park/wake ping-pong switch cost, default vs migration.
2. Memory and GC pause time at one million spawned fibers.
3. Cross-hub channel handoff throughput under the global-queue lock
   (the big_100 p87/p43/p47 cases the hub_submit comments cite).

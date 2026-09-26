/* mn_sched.h -- M:N scheduler skeleton for Phase C.
 *
 * Target: free-threaded Python 3.14+.  N OS threads, each owning a
 * scheduler hub; fibers created on any thread go into a hub's
 * local ring queue.  When a hub's ready queue is empty, it tries to
 * steal from a neighbouring hub's queue tail (Chase-Lev work-stealing
 * deque).  Multiple hubs run Python code in parallel because the
 * GIL is gone in free-threaded builds.
 *
 *   runloom_mn_init(n_threads)      start N OS threads, each with a hub
 *   runloom_mn_fiber(callable)         spawn on the calling thread's hub
 *                                (or, if not in a hub, round-robin)
 *   runloom_mn_run()                join all hubs after their queues drain
 *   runloom_mn_fini()               teardown
 *
 * Design notes (NOT IMPLEMENTED YET -- this header is the spec):
 *
 *   Run queue per hub: Chase-Lev deque.  Owner pushes/pops the tail
 *   (lock-free); thieves pop the head with CAS.  Standard work-
 *   stealing primitive; ~150 LoC of careful atomics in C.
 *
 *   Global fiber pool: thread-safe stack of fresh G structs so
 *   runloom_mn_fiber from outside any hub can place a g without contending.
 *
 *   Sleep heap: still per-hub.  Sleep duration includes a check for
 *   cross-hub wakeups (no -- gs cannot migrate; sleep is hub-local).
 *
 *   Netpoll: one epoll_fd shared across hubs; each hub adds parks to
 *   it.  pump() runs in any hub when its local queue is empty and
 *   wakes whichever hub's g was parked.
 *
 *   Goroutine placement.  There are now TWO modes; the default is the one
 *   this header originally described, and migration is opt-in on top of it.
 *
 *   DEFAULT (stock CPython, no flag).  A g is created on a hub and runs ONLY
 *   on that hub.  Greenlets / our coros have absolute stack pointers that tie
 *   them to a single OS thread.  Work-stealing steals only READY ("fresh",
 *   snap.valid==0) fibers -- they have NEVER run, so there is no stack/tstate
 *   to migrate: the stealer runs them clean from the start on its OWN tstate,
 *   and if such a fiber later parks it parks AND resumes on that same hub.
 *   A woken fiber goes to its origin hub's local FIFO, never the stealable
 *   deque.  No migration of a suspended fiber, ever.
 *
 *   MIGRATION (opt-in: RUNLOOM_MIGRATION=1 / runloom.enable_migration()).
 *   Each fiber carries its OWN PyThreadState, so a suspended fiber is no
 *   longer bound to one hub: a woken fiber is pushed to the process-global
 *   run-queue and resumed by whichever hub drains it (see the global-runq
 *   block in mn_sched_runq.c.inc).  That is what lets an idle hub rescue the
 *   woken work of a hub wedged in a blocking C call -- the failure the
 *   default mode cannot recover from.  Fresh-fiber work-stealing is unchanged
 *   and still runs alongside it.  A wake performed on a general hub thread
 *   skips the global queue: the g goes onto the waker's own deque (Go-style
 *   local wake, runloom_mn_woken_enqueue) where the waker's hub or any idle
 *   thief picks it up; the global queue serves foreign-thread wakers, offload
 *   hub wakers, pinned fibers, replay, and a full deque.
 *
 *   Why migration needs a patched interpreter.  In STOCK free-threaded
 *   CPython it is unsound, for two independent reasons, and either alone
 *   still corrupts:
 *     - ALLOCATION: one _PyThreadState_GET() serves both the allocator and
 *       execution, so a migrated fiber allocates on the origin hub's heap
 *       (mimalloc heap->thread_id mismatch -> _mi_page_retire corruption).
 *       Fixed by Py_TSTATE_ALLOC_HOME.
 *     - EXECUTION: the compiler may hoist/CSE the two reads identifying the
 *       OS thread a frame runs on, so a resumed fiber keeps using the origin
 *       hub's tstate (freed outright if that hub exited) and mis-resolves
 *       _Py_IsOwnedByCurrentThread(), routing decrefs down the non-atomic
 *       ob_ref_local path from the wrong thread -> use-after-free.  Fixed by
 *       Py_TSTATE_EXEC_HOME.  An arm64 SIGSEGV that looks benign on x86-TSO,
 *       so it survives local x86 review and only dies on weak memory.
 *   Both patches, the build recipe, and the measured validation live in
 *   src/patches/README.md.  runloom.migration_available() reports whether
 *   this build has both; with both, migration enables with no override.
 *   Missing either, a migration request is GATED OFF (warn + run the default
 *   scheduler) unless RUNLOOM_ALLOW_UNSAFE_MIGRATION=1 -- dev/fuzzing only.
 *
 *   Historical note: the old "handoff-rescue" pool (run a wedged hub's fibers
 *   on a standby thread) was REMOVED (2026-06) because it migrated suspended
 *   fibers with none of the above in place, and was redundant with
 *   work-stealing anyway (idle hubs already drain a wedged hub's FRESH fibers
 *   safely).  The snap-migration machinery it relied on (the cross-hub snap
 *   re-root + the RUNLOOM_DIAG_MIGRATE tripwire) has since been removed too,
 *   now that per-g-tstate is the only migration path.
 *
 *   Wake interrupts: when a hub steals work, it needs to inform other
 *   hubs that may be sleeping in epoll_wait.  Use eventfd / pipe
 *   per hub.
 */
#ifndef RUNLOOM_MN_SCHED_H
#define RUNLOOM_MN_SCHED_H

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>

#include "runloom_sched.h"   /* for runloom_g_t forward */

/* Forward-decl avoids pulling io_uring.h into every translation unit
 * that includes mn_sched.h. */
struct runloom_iouring_ring;

/* offload_hubs: how many hubs to reserve for blocking offload.  -1 = consult
 * RUNLOOM_OFFLOAD_HUBS (the default); >=0 overrides it.  An explicit argument
 * wins so a library can ask for what its own code needs without requiring its
 * host to set an environment variable.  See runloom_mn_offload_fiber. */
int runloom_mn_init(int n_threads, int offload_hubs);
/* stack_size: per-fiber C-stack override in bytes; 0 = the hub default.
 * Use a larger value for a g that runs a deep, non-yielding C burst (cold
 * imports, terminfo/OpenSSL init) that the copy-grow can't rescue mid-burst. */
PyObject *runloom_mn_fiber(PyObject *callable, size_t stack_size);
/* Spawn on a RESERVED OFFLOAD hub (RUNLOOM_OFFLOAD_HUBS), where a blocking call
 * may run without stranding the g's woken on a general hub.  Raises
 * RuntimeError when none are reserved -- it never silently falls back to a
 * general hub.  runloom_mn_offload_hub_count() reports how many exist (0 =
 * feature off). */
PyObject *runloom_mn_offload_fiber(PyObject *callable, size_t stack_size);
int runloom_mn_offload_hub_count(void);
/* Place the fiber on hub `hub_id`, drained to that hub's local FIFO rather than
 * its stealable deque.  hub_id < 0 or >= the live hub count raises ValueError.
 * Alone among spawn paths it may name a reserved offload hub, so a test can
 * force general work onto one; on a busy one the fiber strands behind the
 * blocking call.
 *
 * PIN CONTRACT: a pinned fiber is NOT stealable.  It runs only when its hub
 * does, so it starves if that hub blocks -- a determinism knob for tests, not
 * an affinity feature. */
PyObject *runloom_mn_fiber_pinned(PyObject *callable, size_t stack_size,
                                  int hub_id);

/* Confine `g`'s resumes to hub `hub_id` (<0 clears).  Returns 0, or -1 with a
 * Python error set.  Exposed as G.pin(hub).  A cross-hub target is only sound
 * under a migration mode, so elsewhere it raises RuntimeError; pinning to its
 * own hub is always legal.  Pin contract: runloom_mn_fiber_pinned above. */
int runloom_mn_pin_for_wake(runloom_g_t *g, int hub_id);

/* Like runloom_mn_fiber but `size` is a grow-down LEARNED size: spawn it down the
 * deferred (lazy) stack-alloc path so a tight front-load loop doesn't cold-mmap a
 * guarded stack per spawn -- the alloc lands on the consumer hub where the pool
 * recycles -- while still installing exactly `size`.  For internal right-sizing
 * only (the C-side frozen grow-down), never a user pin. */
PyObject *runloom_mn_fiber_grown(PyObject *callable, size_t size);
/* Bulk-spawn n fibers all running `callable`, looping the spawn core in C
 * (skips n Python->C dispatches + per-call arg parsing).  indexed != 0 calls
 * each as callable(i) for i in 0..n-1 (per-fiber arg); 0 calls callable().
 * Returns 0, or -1 with a Python error set on partial failure (already-created
 * fibers still run). */
int runloom_mn_fiber_n(PyObject *callable, long n, size_t stack_size, int indexed);
/* C-only spawn: no Python callable, just a function + arg.  Distributes
 * fibers across hubs round-robin (same as runloom_mn_fiber).  Returns 0 on
 * success, -1 with errno on failure (ENOMEM, EINVAL). */
int runloom_mn_fiber_c(runloom_c_entry_fn fn, void *arg);
Py_ssize_t runloom_mn_run(void);
void runloom_mn_fini(void);

/* Reset the M:N scheduler in a forked child (the hub threads are gone).
 * Abandons the inherited hubs, zeroes the pending counter so runloom_mn_run
 * can't hang on dead hubs, and re-inits the global run-queue lock.  After
 * this mn_hub_count()==0 and a fresh runloom_mn_init() works.  Single-thread
 * child only (called from the after-fork handler). */
void runloom_mn_reset_after_fork(void);

/* Current M:N session generation -- monotonically bumped on every hub-pool
 * teardown (runloom_mn_fini / reset_after_fork).  A RunloomG handle stamps this
 * at creation; RunloomG.wake compares to decide whether the g's park_hub still
 * points at a live hub.  See runloom_mn_gen in mn_sched.c. */
uint64_t runloom_mn_generation_get(void);

/* Logical clock for the controlled-replay scheduler (RUNLOOM_MN_SEED + barrier).
 * Returns the deterministic logical time that sched_sleep deadlines and timer
 * firing are measured against; `fallback` (a wall-clock value) is returned when
 * controlled mode is off, so callers stay wall-clock in production. */
double runloom_mn_logical_now_or(double fallback);

/* ns-native census clock (MN_SIM_DST_PLAN.md I1): the EXACT ns authority the
 * sim ready ledger stamps deliver_at from; `fallback` is returned when
 * controlled mode is off.  runloom_mn_logical_reset zeroes it -- the
 * runloom_sim_reset cross-TU hook (all clock planes reset between runs). */
long long runloom_mn_logical_ns_or(long long fallback);
void runloom_mn_logical_reset(void);

/* Is the controlled-replay scheduler LIVE (enabled + barrier + armed)?  The
 * cross-TU gate the sim plane keys on: under sim+mn the netpoll pump no-ops
 * (readiness flows only from the census dispatch) and wait_fd enforces the
 * sim conn registry.  MN_SIM_DST_PLAN.md I2. */
int runloom_mn_ctrl_armed(void);

/* Did ctrl_init establish controlled+barrier mode?  mn_init's effective-state
 * fence check (catches ctrl_init's silent OOM self-disable). */
int runloom_mn_ctrl_controlled(void);

/* Is the wall-clock preempt time-slicer thread running?  mn_init's fence
 * check: a slicer started BEFORE the sim env was set keeps posting
 * nondeterministic yields into a seeded mn-sim run. */
int runloom_preempt_active(void);

/* Phase C v2 hook.  Called from runloom_sched_yield to give the M:N
 * scheduler a chance to handle the yield in hub context.  Returns
 * 1 if we're inside a hub and the yield was handled (g re-queued on
 * the hub's local FIFO, state snapped, asm-yield done, control will
 * return when hub re-resumes g).  Returns 0 if we're not in a hub
 * and the caller should fall through to the single-thread sched path. */
int runloom_mn_yield_current(void);

/* Returns the number of M:N hubs currently running (0 if mn_init was
 * never called or after mn_fini). */
int runloom_mn_hub_count(void);

/* R0 gauges (lock-free per-hub census): live in-scheduler gs (submitted minus
 * completed, sum conserved under work-stealing); cumulative retired gs
 * (odometer); fresh stealable gs across all hub deques. */
long      runloom_mn_pending_total(void);
long long runloom_mn_completed_total(void);
long      runloom_mn_deque_depth_total(void);

/* ---- per-hub diagnostic snapshot (runloom.inspect.hubs()) ----
 * A point-in-time view of every hub's scheduler state, for answering
 * "what is each hub doing / is any hub wedged, on what, for how long".
 * Every field is a lock-free read of a per-hub atomic; for a hub that is
 * DETACHED-wedged (a fiber inside a non-cooperative blocking call) it
 * ALSO best-effort fills `blocked_at` with the running fiber's top
 * Python frame -- the blocking call site (see mn_sched_hubinfo.c.inc
 * for the safety argument). */
/* sysmon hazard pointer (mn_sched_sysmon.c.inc).  A per-g tstate's deleter
 * calls retire_wait BEFORE PyThreadState_Clear so the watchdog never reads a
 * freed tstate; hub_attach_state is the watchdog's (and hubinfo's) safe read. */
struct runloom_hub;                       /* defined in mn_sched.c */
void runloom_sysmon_tstate_retire_wait(PyThreadState *ts);
int  runloom_sysmon_hub_attach_state(struct runloom_hub *h, PyThreadState *hts);

typedef struct runloom_hub_info {
    int       id;                 /* dense hub index 0..count-1 */
    long long running_g;          /* goid of the g currently being resumed */
    int       has_running_g;      /* 0 if idle / sysmon instrumentation off */
    double    dwell_ms;           /* how long the current resume has run, or 0 */
    int       attach_state;       /* RUNLOOM_TS_DETACHED/ATTACHED/SUSPENDED, -1 unknown */
    long      pending;            /* gs owned + queued on this hub */
    int       preempt_requested;  /* sysmon has asked this hub to yield */
    int       instrumented;       /* 1 if sysmon resume-tracking is live */
    char      blocked_at[192];    /* "qualname (file:line)" best-effort, or "" */
} runloom_hub_info_t;

/* Snapshot every live hub.  Returns a malloc'd array of `*count_out` entries
 * (caller frees with free()), or NULL with *count_out=0 when the M:N
 * scheduler is not running.  Normal interpreter context only -- it may touch
 * Python frame objects to fill blocked_at. */
runloom_hub_info_t *runloom_mn_hub_snapshot(long *count_out);

/* Return an opaque handle to the hub running on this thread (or NULL
 * if the calling thread isn't a hub).  Used by netpoll to record where
 * to route a parked g when it becomes ready. */
void *runloom_mn_current_hub_opaque(void);

/* Persistent PyThreadState for a runloom-owned worker OS thread (e.g. a
 * blockpool offload worker).  Created serialized against the hub-startup
 * immortalize race and bound to the M:N interpreter, returned DETACHED.  The
 * worker attaches it (PyEval_RestoreThread) ONLY around the Python call it runs,
 * so it pays no per-job tstate create/destroy -- the runtime HEAD_LOCK churn
 * that otherwise serializes all offload workers and caps offload throughput.
 * Returns NULL when the M:N runtime isn't up (caller falls back to a per-call
 * PyGILState_Ensure).  MUST be released on the SAME thread that created it, via
 * runloom_mn_worker_tstate_delete (gilstate-TSS is thread-bound). */
PyThreadState *runloom_mn_worker_tstate_new(void);
void runloom_mn_worker_tstate_delete(PyThreadState *ts);

/* Deposit an io_uring single-op cancel request (the target op, a void* across
 * the TU boundary) into the owning hub's mailbox + wake it, so the hub -- the
 * SINGLE_ISSUER of its ring -- submits the ASYNC_CANCEL at its loop top.
 * Returns 1 if accepted, 0 if the slot is busy (best-effort, dropped). */
int runloom_mn_hub_request_iouring_cancel(void *hub_opaque, void *op);

/* Map a hub_opaque (as returned by runloom_mn_current_hub_opaque, or
 * stashed on a parker/g) to the dense 0..hub_count-1 hub id.  Returns
 * -1 for NULL (single-thread sched).  Used by netpoll's per-hub
 * parker pool selector to look up the right pool. */
int runloom_mn_hub_id_of(void *hub_opaque);

/* Return the fiber currently running on this thread's hub (or
 * NULL if not in a hub or no g is running).  Netpoll's wait_fd uses
 * this -- it can't read runloom_sched_t::current because that's the
 * single-thread sched's slot, not the per-hub slot. */
runloom_g_t *runloom_mn_tls_current_g(void);

/* Signal hub_main "don't requeue the current g on return" -- used by
 * the park path (netpoll, channels) where the parker takes ownership
 * and arranges its own wake.  Without this, hub_main's "g yielded but
 * didn't self-queue, must be a raw yield" fallback re-pushes the g to
 * the local FIFO and the next iteration re-runs it -> busy loop. */
void runloom_mn_tls_mark_parked(void);

/* Signal hub_main / the barrier census "the segment about to yield parked on a
 * FOREIGN-thread completion" (blocking-IO / offload).  Set by runloom_park_generic
 * on the hub path when foreign_wakeable!=0; read at the resume boundary
 * (ctrl_release) so the census marks this hub non-participating for the round.
 * No-op outside the controlled+barrier seeded scheduler. */
void runloom_mn_tls_mark_parked_foreign(void);

/* Return the runloom_sched_t owned by the hub running on this thread, or
 * NULL if not in a hub.  Used by hub-aware sched primitives (e.g.,
 * sleep_until) so they push to the hub's per-thread sleep heap rather
 * than the global single-thread heap. */
runloom_sched_t *runloom_mn_current_sched(void);

/* Wake g back to its original hub (or to the global single-thread
 * sched if hub_opaque is NULL).  Thread-safe; can be called from any
 * thread (typically netpoll pump on whichever hub did epoll_wait).
 * For hubs: pushes onto the target hub's submission list under
 * sub_lock; hub_main drains submissions each iteration and dispatches
 * routes them to the deque (if g is fresh) or local FIFO (if yielded). */
void runloom_mn_wake_g(void *hub_opaque, runloom_g_t *g);

/* Idle-stack-sweep handshake for RUNLOOM_PER_G_TSTATE (no-op-safe to call in
 * either mode; the sweep caller gates them on per-g-tstate).  try_claim CASes
 * the g's wake_state PARKED -> SWEEPING and returns 1 if it won exclusive
 * ownership of the g's stack for an MADV_DONTNEED, 0 if the g was concurrently
 * woken/owned (skip it).  claim_release ends that ownership: SWEEPING -> PARKED,
 * or, if a wake landed during the madvise, SWEEPING_WOKEN -> QUEUED and
 * re-enqueues it onto the global run-queue exactly once (so the deferred wake is
 * never lost).  See the wake_state field comment in runloom_sched.h. */
int  runloom_mn_sweep_try_claim(runloom_g_t *g);
void runloom_mn_sweep_claim_release(runloom_g_t *g);

/* The current hub's per-thread io_uring ring (NULL if not in a hub,
 * or the hub failed to create its ring at startup -- callers should
 * fall back to the global ring path).  Used by runloom_iouring_recv /
 * _send to dispatch to the hub's SINGLE_ISSUER ring instead of the
 * global ring's mutex-protected submit + legacy spin-drain. */
struct runloom_iouring_ring *runloom_mn_current_iouring_ring(void);

/* Halt the M:N sysmon watchdog + disable preemption from inside the
 * fatal-signal crash handler.  Async-signal-safe: only atomic/plain stores to
 * the loop-stop flag + the preempt-enable flag.  A hub thread that has faulted
 * and is driving the crash dump must NOT have its faulting g preempted away
 * before the handler's chain-out re-faults and cores, leaving the process
 * limping (service dead, no core).  See runloom_crash.c / crash_handler. */
void runloom_sched_freeze_for_crash(void);

/* CHESS schedule-explorer conflict tracking (tools/mn_controlled/chess_explore.py
 * --dpor partial-order reduction): when a schedule is being DRIVEN, record the
 * shared object (a Chan) each baton segment touches, so the driver can compute
 * the independence relation (disjoint-object segments commute -> reorderings are
 * equivalent -> pruned).  runloom_mn_seg_track is 0 in production -- one
 * predicted-not-taken branch per chan op -- and seg_touch is a no-op unless a
 * schedule drive armed it.  Cross-TU: the chan ops (chan.c) record into the
 * baton controller (mn_sched.c). */
extern int runloom_mn_seg_track;
void runloom_mn_seg_touch(unsigned long long obj_id);

/* Controlled-mode stream for select's uniform-pseudo-random case order.
 * Returns the next draw from a stream seeded off RUNLOOM_MN_SEED, or 0 when
 * the controlled scheduler is off (the caller then uses its own per-thread
 * ASLR-seeded stream -- fine in production, nondeterministic by construction
 * under a replay seed).  Cross-TU: consumed by chan_select_helpers.c.inc
 * (chan.c), lives with the baton controller (mn_sched.c).  Only ever drawn
 * inside baton-held segments, so the stream needs no locking and its draw
 * order is the deterministic segment order. */
uint64_t runloom_mn_ctrl_select_rand(void);

/* LDFI -- lineage-driven fault injection (tools/mn_controlled/chess_ldfi.py): DROP
 * the runloom_ldfi_drop-th chan wake (RUNLOOM_LDFI_DROP) to test whether that wake
 * is load-bearing (dropping it strands a fiber -> hang) or redundant (a backup
 * path still completes the run).  -1 = off (one predicted branch per wake in
 * production).  runloom_ldfi_count counts wakes so the driver can enumerate them;
 * written to RUNLOOM_LDFI_COUNT at fini.  Use under the seeded baton so the wake
 * order is serialized + reproducible. */
#define RUNLOOM_LDFI_MAX_DROPS 64
extern int runloom_ldfi_drop_set[RUNLOOM_LDFI_MAX_DROPS]; /* wake indices to drop (a CUT SET) */
extern int runloom_ldfi_dropn;     /* size of the cut set; 0 = off (depth>1 = |set|>1) */
extern int runloom_ldfi_count;     /* wakes seen so far (for the driver to enumerate) */

#endif /* RUNLOOM_MN_SCHED_H */

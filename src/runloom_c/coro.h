/* coro.h -- portable stackful coroutines.
 *
 * Three properties matter:
 *   1. Each runloom_coro owns its own C stack (heap-allocated, fixed at create).
 *   2. runloom_coro_resume / runloom_coro_yield transfer control with no syscall.
 *   3. The current coroutine pointer is thread-local, so multiple OS
 *      threads each run their own scheduler independently.
 *
 * Backend is chosen at compile time via plat.h:
 *   - fcontext: hand-rolled asm context switch (x86_64 / aarch64).
 *   - ucontext: getcontext / makecontext / swapcontext.  POSIX.1-2001.
 *               Still works on every Unix we care about despite being
 *               deprecated by POSIX.1-2008.
 *
 * The public API is the same on both.
 */
#ifndef RUNLOOM_CORO_H
#define RUNLOOM_CORO_H

#include "compat.h"

typedef struct runloom_coro runloom_coro_t;

typedef void (*runloom_entry_fn)(void *user);

/* Lifecycle: returns NULL on alloc failure (errno set). */
runloom_coro_t *runloom_coro_new(size_t stack_size,
                           runloom_entry_fn entry,
                           void *user);

void runloom_coro_destroy(runloom_coro_t *c);

/* Switch into the coroutine.  Must be called from the same OS thread on
 * which runloom_coro_new was called.  Returns when the coroutine yields or
 * returns.  Calling resume on a done coroutine is undefined. */
void runloom_coro_resume(runloom_coro_t *c);

/* Optional pre-swap hook: invoked (when non-NULL) by every runloom_coro_resume
 * backend right before switching into the fiber, AFTER any resume-boundary stack
 * grow.  The Python layer registers this on free-threaded 3.14+ to re-arm the
 * live thread state's C-stack soft/hard limit at THIS fiber's stack -- 3.14's
 * overflow check is SP-vs-soft_limit, and the default shared-hub tstate's limit
 * is otherwise left pointing at whichever fiber entered last.  NULL = no-op. */
extern void (*runloom_coro_pre_swap)(runloom_coro_t *c);

/* Yield from inside a coroutine.  Returns control to whatever called
 * runloom_coro_resume on us; on next resume, execution continues just
 * past the runloom_coro_yield call.  Calling yield from outside any
 * coroutine is undefined. */
void runloom_coro_yield(void);

/* Predicates. */
int runloom_coro_done(const runloom_coro_t *c);

/* This coro's stack size in bytes.  Used by the fiber dump. */
size_t runloom_coro_stack_size(const runloom_coro_t *c);

/* Lowest usable byte of this coro's stack (the PROT_NONE guard page is the page
 * immediately below it).  Used by the crash handler to map a faulting address
 * back to a fiber. */
void *runloom_coro_stack_base(const runloom_coro_t *c);

/* Size in bytes of the guard page below each coro stack. */
size_t runloom_coro_guard_size(void);

/* Turn park-time idle-page reclaim (runloom_coro_park) on/off.  Off by default;
 * the stack auto-sizer enables it so that starting fibers large stays RSS-free. */
void runloom_coro_park_reclaim_set(int on);

/* Backend identifier ("fcontext-asm", "ucontext"); useful for tests. */
const char *runloom_coro_backend(void);

/* R0 gauges (lock-free): live depot-backed C-stacks IN USE, and freed stacks
 * retained in the shared cross-hub depot.  0 on backends without a depot. */
long runloom_coro_stack_live(void);
long runloom_coro_depot_pooled(void);

/* Per-thread setup / teardown.  Must be called once per OS thread
 * before any coro on that thread.  Idempotent. */
void runloom_coro_thread_init(void);
void runloom_coro_thread_fini(void);

/* Pre-warm the stack pool with n pre-mmaped stacks of the given
 * size.  Eliminates the first-spawn mmap stall for servers that
 * know they're about to spawn a known number of fibers.
 * No-op if n <= 0.  Returns the number actually pre-allocated. */
int runloom_coro_warmup(size_t stack_size, int n);

/* Prewarm `n` stacks into the GLOBAL depot (cross-hub, unlike warmup's per-thread
 * cache).  background=1 runs it on a detached OS thread and returns 0 immediately
 * (the spawn burst then pops instead of mmap'ing); background=0 runs synchronously
 * and returns the count retained (-1 if a background thread couldn't start).
 * Bounded by the depot cap (RUNLOOM_STACK_DEPOT_CAP) -- raise it near the target
 * for a large prewarm. */
int runloom_coro_prewarm(size_t stack_size, int n, int background);

/* CONTINUOUS prewarm daemon: keep the GLOBAL depot topped to `target` stacks so a
 * spawn burst always finds a ready backlog (it refills as the pool drains, idling
 * when full).  One daemon per process: _keep starts it or re-targets a running
 * one (target<=0 stops it); _stop halts + joins it.  Returns 0 ok / -1 if the
 * thread couldn't start.  _reset_after_fork zeroes the
 * (copied, threadless) daemon state in a fork child. */
int  runloom_coro_prewarm_keep(size_t stack_size, int target);
void runloom_coro_prewarm_stop(void);
void runloom_coro_prewarm_reset_after_fork(void);

/* Depot auto-cap: the stack pool sizes itself to the live-stack high-water-mark
 * (no RUNLOOM_STACK_DEPOT_CAP needed).  _init resolves SAFE_MAX once (mn_init);
 * _tick decays the watermark + recomputes the cap (called once per sysmon tick);
 * _reset forgets the watermark (mn_fini + fork child). */
void runloom_stack_autocap_init(void);
void runloom_stack_autocap_tick(void);
void runloom_stack_autocap_reset(void);

/* Drop the physical page frames of c's currently-idle (low) stack
 * region without releasing the stack -- the coro stays bound to its
 * fiber.  The scheduler calls this when a g parks on a waiter
 * (netpoll/chan/sleep/park_safe); the next resume re-faults the few
 * touched pages (~one page fault).  MUST be called only while c is
 * SUSPENDED (so its saved stack pointer is valid).  No-op unless the
 * auto-sizer enabled it (runloom_coro_park_reclaim_set), and on backends
 * without an inspectable saved SP (ucontext).
 *
 * M:N SAFETY: race-free against a concurrent resume even though a netpoll
 * parker is wakeable (commit==PARKED) before its yield returns control
 * here.  A pump on another hub only *claims + re-queues* the g; it never
 * resumes it.  The wake routes to the g's OWNING hub (netpoll.c:1693,
 * runloom_mn_wake_g(p->hub, ...)), and a woken g (snap.valid) lands in that
 * hub's LOCAL ready FIFO, which is never work-stolen (only the Chase-Lev
 * deque is stealable -- mn_sched.c:248-286).  So the sole thread that
 * resumes g is the same hub that runs this madvise at its post-resume
 * site: madvise happens-before the next resume on one thread, and no
 * other hub ever touches the stack.  It stays off outside autosize only
 * for the throughput cost (madvise+refault per park hurts short-park
 * churn), not for safety.  See HANDOFF. */
void runloom_coro_park(runloom_coro_t *c);

/* Unconditional variant: madvise c's below-SP idle pages with no reclaim
 * gate.  Used by the hub-idle dwell-based sweep, which does its own gating
 * + threshold.  Same SUSPENDED + owning-hub safety contract as
 * runloom_coro_park. */
void runloom_coro_madvise_idle(runloom_coro_t *c);

/* ------------------------------------------------------------------ */
/* Stack-usage measurement (used by sched calibration)                */
/* ------------------------------------------------------------------ */

/* When painting is enabled, every runloom_coro_new paints the stack body
 * with a known sentinel pattern (8-byte chunks).  runloom_coro_scan_hwm
 * then walks low->high and reports how many bytes were actually
 * touched by the coroutine.
 *
 * Disable painting (e.g. after calibration) to drop the per-spawn
 * paint cost (~few µs at 256 KB). */
void runloom_coro_paint_set(int enabled);
int  runloom_coro_paint_enabled(void);

/* Opt-in security scrub of recycled fiber stacks (default off). */
void runloom_coro_scrub_set(int enabled);
int  runloom_coro_scrub_enabled(void);

/* Returns the high-water mark in bytes (deepest write detected by
 * scanning for the sentinel).  Returns 0 if painting was disabled or
 * the coro hasn't been used. */
size_t runloom_coro_scan_hwm(runloom_coro_t *c);

/* After fork(): re-init the FCONTEXT coro cross-hub balance lock in the child
 * (no-op on non-FCONTEXT backends).  Wired into runloom_after_fork_child. */
void runloom_coro_reset_after_fork(void);

#endif /* RUNLOOM_CORO_H */

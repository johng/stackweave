/* runloom_fiberlock.c -- a Python-level lock's wait parks the fiber.
 *
 * _thread.lock and _thread.RLock wait in CPython's parking lot, which sleeps
 * the OS thread on a semaphore.  Under M:N that thread is a hub, and every
 * fiber waiting for a lock a parked fiber holds takes a hub with it: with as
 * many waiters as hubs nothing can run the holder (a hard hang the deadlock
 * detector cannot see, since the hubs are busy, not idle).  The exec-home
 * patch's parking_lot.c hunk delegates the sleep to these hooks for a wait
 * flagged by the lock objects (_PY_LOCK_FIBER): the parking lot keeps its
 * queue, handoff and timeout-vs-unpark bookkeeping, and only the sleep and the
 * wakeup change.  A fiber parks through the same in-memory park every
 * cooperative primitive uses (runloom_park_generic, foreign-wakeable: the
 * unparker may be a plain OS thread), so its hub runs other fibers meanwhile
 * and the wake can come from any thread.  A plain thread, the hub's own stack
 * and a nested run(1)'s fibers keep the platform semaphore.
 *
 * Wake protocol (sema->fiber_state): 0 waiting, 1 WAKING, 2 WOKEN.  wake()
 * stores WAKING, wakes the fiber (runloom_sched_wake_safe), stores WOKEN and
 * never touches the entry again; the waiter leaves only once it reads WOKEN,
 * so the entry (on the waiter's stack) outlives every access.  WAKING is
 * stored BEFORE the wake: stored after, a fiber the wake resumed could re-park
 * on a state still 0 with nobody left to wake it.  A wake that found the fiber
 * running leaves a wake_pending credit its next, unrelated park would take for
 * its own wake; every credit outstanding when a wait ends was aimed at this
 * wait (a fiber waits for one thing at a time, and the next primitive has not
 * registered yet), so the waiter takes them all back.
 *
 * Each waiter is on the list of the sched it waits on (sched->fl_head: a
 * hub's for an M:N fiber, its owner's for a run(1) fiber), linked through its
 * g -- not its stack, so dropping a woken-but-unrun g can unlink it
 * (runloom_fiberlock_forget, from the g's free path).  The list's fl_lock is
 * per sched: contended waits on different hubs never share it.  A wake holds
 * it from before it queues the fiber until after it stores WOKEN, so the g
 * cannot be freed (its stack with the entry) under the wake.  The lists serve:
 *   - Teardown.  An M:N run() that ends abnormally (a signal carried out of
 *     mn_run, a deadlock raise) leaves fibers parked here, their entries
 *     still in the parking lot, and frees the hubs.  mn_fini ends the session
 *     first thing (runloom_fiberlock_mn_stop: from then on a wake for one of
 *     its waiters is dropped, never queued on a hub) and dequeues the waiters'
 *     entries right away, so a release no longer hands a lock to a fiber that
 *     will never run; once the hubs are joined it waits out any wake still
 *     running (fl_inflight, a Dekker pair with fl_alive), unlinks the waiters
 *     (a late one too) and gives back their run-alive counts.  Still open: a
 *     release in the short window between run() returning and mn_fini, or one
 *     that reaches a fiber which started waiting while the hubs stopped, hands
 *     off to a fiber that never runs, and that lock stays locked.
 *   - Signals, on the single-thread plane.  A stock thread's lock wait on the
 *     main thread returns to run the signal handler (Ctrl-C interrupts
 *     lock.acquire()).  A run(1) fiber's wait parks instead, so the drain,
 *     finding a signal pending while such a waiter exists, wakes it with a
 *     mark; the wait returns Py_PARK_INTR and CPython runs the handler in the
 *     fiber, exactly where the thread would have.  Only for a wait flagged
 *     _Py_PARK_INTR (the lock objects' acquire).  M:N fibers never run on the
 *     main thread, so their waits are as uninterruptible as a stock non-main
 *     thread's; the signal is carried out of run(), as for any wait that is
 *     not a signal recipient.
 *   - Fork.  The child keeps only the forking thread, and CPython empties the
 *     parking lot (_PyParkingLot_AfterFork).  That thread's run(1) waiters
 *     survive, so they are woken to retry the lock with a fresh entry; every
 *     other waiter is dead, and its run-alive count is given back.
 *
 * A timed wait arms one timer entry (re-armed only after a migration) and,
 * when it ends, bumps g->timer_ticket: the timer heap then drops the entry
 * instead of firing it into the fiber's next park, and purges it before its
 * deadline (runloom_sched_drain_timers), so a loop of acquire(timeout=3600)
 * neither grows the heap nor pins the g.
 *
 * The prepare hook names a fiber only when parking is sound: on the fiber's
 * own coroutine (not a nested _Coro it resumed, which would leave the outer g
 * marked parked while only the Coro yields), with its own thread state
 * attached (not a subinterpreter's, inside _interpreters.exec), and not inside
 * object destruction (a trashcan unwind or a biased-refcount merge -- the same
 * gate the preemption yield sites honour).  Anything else blocks the thread,
 * as stock.
 *
 * This TU includes CPython's internal semaphore and parking-lot headers for
 * the hook table, as runloom_iframe.c does for the frame layout. */
#ifndef Py_BUILD_CORE_MODULE
#  define Py_BUILD_CORE_MODULE 1
#endif
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdlib.h>
#include <string.h>
#include <sched.h>
#include <time.h>

#include "runloom_sched.h"
#include "mn_sched.h"
#include "runloom_iframe.h"
#include "coro.h"
#include "plat_compat.h"

#ifdef _Py_FIBER_PARK_HOOKS
#  include "internal/pycore_semaphore.h"
#  include "internal/pycore_parking_lot.h"   /* Py_PARK_*, _Py_PARK_*, UnparkAll */

#ifndef _Py_PARK_INTR
#  define _Py_PARK_INTR 0     /* an interpreter from before the flag: never set */
#endif

enum { FL_WAITING = 0, FL_WAKING = 1, FL_WOKEN = 2 };

static unsigned long long fl_parks, fl_wakes, fl_stale_wakes, fl_timer_arms,
                          fl_signal_wakes;

/* The interpreter whose fibers these are: the one that imported stackweave_c.
 * The hooks are process-wide, but a lock in a subinterpreter is waited for
 * with that interpreter's thread state attached. */
static PyInterpreterState *fl_interp;

/* The live M:N session: fl_session is bumped at each mn_init (fl_alive set
 * then) and fl_alive cleared first thing in mn_fini.  fl_inflight counts the
 * wakes between their fl_alive check and their last touch of a waiter; the
 * teardown waits for it to drain before the hubs go.  fl_waiting counts linked
 * waiters, each holding one run-alive (foreign-park) count while parked. */
static uint64_t fl_session;
static int fl_alive;
static long fl_inflight;
static long fl_waiting;

static void fl_link(runloom_g_t *g, runloom_sched_t *L)
{
    runloom_mutex_lock(&L->fl_lock);
    g->fl_prev = NULL;
    g->fl_next = L->fl_head;
    if (L->fl_head != NULL) L->fl_head->fl_prev = g;
    L->fl_head = g;
    __atomic_store_n(&g->fl_list, L, __ATOMIC_RELEASE);
    runloom_mutex_unlock(&L->fl_lock);
    __atomic_add_fetch(&fl_waiting, 1, __ATOMIC_RELAXED);
}

/* Caller holds L->fl_lock and g is on L. */
static void fl_unlink_locked(runloom_g_t *g, runloom_sched_t *L)
{
    if (g->fl_prev != NULL) g->fl_prev->fl_next = g->fl_next;
    else                    L->fl_head = g->fl_next;
    if (g->fl_next != NULL) g->fl_next->fl_prev = g->fl_prev;
    g->fl_prev = g->fl_next = NULL;
    __atomic_store_n(&g->fl_list, (runloom_sched_t *)NULL, __ATOMIC_RELEASE);
    __atomic_sub_fetch(&fl_waiting, 1, __ATOMIC_RELAXED);
}

static int fl_unlink(runloom_g_t *g)
{
    int done = 0;
    runloom_sched_t *L = __atomic_load_n(&g->fl_list, __ATOMIC_ACQUIRE);
    if (L == NULL) return 0;
    runloom_mutex_lock(&L->fl_lock);
    if (g->fl_list == L) {
        fl_unlink_locked(g, L);
        done = 1;
    }
    runloom_mutex_unlock(&L->fl_lock);
    return done;
}

/* A g on a waiter list is being freed: woken, then dropped before it ran (a
 * sched reset).  Unlinking takes the list's lock, which a wake holds while it
 * still touches the g's stack, so the free waits for it.  Its park never
 * returns, so give back the run-alive count it holds. */
void runloom_fiberlock_forget(runloom_g_t *g)
{
    if (fl_unlink(g))
        runloom_foreign_park_release();
}

static void fl_nap(void)
{
    struct timespec ts = { 0, 50 * 1000 };   /* 50 us */
    nanosleep(&ts, NULL);
}

/* The fiber that would park, or NULL to block the thread (see the header
 * comment): an M:N fiber on its own stack (NULL on the hub's own stack and,
 * through the nest-hidden accessors, in a nested run(1)), or a single-thread
 * run(1)'s current fiber off any hub thread.  Records the wait on the g for
 * the wake hook.  Runs under the parking lot's bucket lock: no allocation,
 * no locks. */
static void *fl_prepare(const void *addr, int flags)
{
    PyThreadState *ts = PyThreadState_GetUnchecked();
    runloom_g_t *g;
    int hub;
    if (ts == NULL || ts->interp != fl_interp)
        return NULL;
    if (runloom_tstate_in_destruction(ts))
        return NULL;
    if (runloom_mn_current_hub_opaque() != NULL) {
        g = runloom_mn_tls_current_g();
        if (g == NULL || g->tstate != ts)
            return NULL;
        hub = 1;
    }
    else {
        if (runloom_mn_hub_fiber_raw() != NULL)
            return NULL;                   /* a nested drain on a hub */
        g = runloom_sched_peek_current();
        if (g == NULL)
            return NULL;
        hub = 0;
    }
    if (g->coro == NULL || g->coro != runloom_coro_current())
        return NULL;                       /* inside a _Coro the fiber resumed */
    g->fl_addr = addr;
    g->fl_hub = (unsigned char)hub;
    g->fl_intr = (unsigned char)((flags & _Py_PARK_INTR) != 0);
    g->fl_session = hub ? __atomic_load_n(&fl_session, __ATOMIC_ACQUIRE) : 0;
    __atomic_store_n(&g->fl_sig, 0, __ATOMIC_RELAXED);
    return g;
}

/* Prepare said fiber but parking failed (OOM arming the timer, or no fiber
 * after all): never block the platform semaphore, which no one will post;
 * nap until the unparker arrives or the deadline passes, detached as the
 * parking lot would have left a thread (it skipped that for a fiber wait), so
 * a stop-the-world does not wait on this thread. */
static int fl_poll(_PySemaphore *sema, double deadline)
{
    int res = Py_PARK_OK;
    PyThreadState *ts = PyEval_SaveThread();
    while (__atomic_load_n(&sema->fiber_state, __ATOMIC_ACQUIRE) == FL_WAITING) {
        if (deadline >= 0.0 && runloom_sched_monotonic_seconds() >= deadline) {
            res = Py_PARK_TIMEOUT;
            break;
        }
        fl_nap();
    }
    PyEval_RestoreThread(ts);
    return res;
}

static int fl_wait(_PySemaphore *sema, PyTime_t timeout_ns)
{
    enum { HOMES = 4 };
    runloom_g_t *g = (runloom_g_t *)sema->fiber;
    runloom_sched_t *L = g->fl_hub ? runloom_mn_current_sched() : g->owner;
    void *homes[HOMES];
    int nhomes = 0;
    double deadline = -1.0;
    void *timer_home = NULL;
    int res = Py_PARK_OK;

    g->fl_sema = sema;
    fl_link(g, L);
    if (timeout_ns >= 0)
        deadline = runloom_sched_monotonic_seconds() + (double)timeout_ns / 1e9;
    for (;;) {
        int prc;
        if (__atomic_load_n(&sema->fiber_state, __ATOMIC_ACQUIRE) != FL_WAITING)
            break;                                  /* the unparker's wake */
        if (__atomic_exchange_n(&g->fl_sig, 0, __ATOMIC_ACQ_REL)) {
            res = Py_PARK_INTR;                     /* the drain's signal mark */
            goto out;
        }
        if (deadline >= 0.0) {
            void *home;
            if (runloom_sched_monotonic_seconds() >= deadline) {
                res = Py_PARK_TIMEOUT;              /* the parking lot dequeues us, or
                                                     * calls back if an unpark is under way */
                goto out;
            }
            /* One timer entry per wait, re-armed only after a move: an entry
             * on the hub the fiber runs on cannot fire before the next park
             * (only that hub fires it, and not while the fiber runs there),
             * while one on a hub left behind may fire, find the fiber running
             * and be discarded. */
            home = runloom_park_timer_home();
            if (home != timer_home) {
                if (runloom_park_timer_arm(deadline, 0) < 0) {
                    PyErr_Clear();                  /* OOM: the lock API cannot report it */
                    res = fl_poll(sema, deadline);
                    goto out;
                }
                timer_home = home;
                if (nhomes < HOMES) homes[nhomes++] = home;
                __atomic_add_fetch(&fl_timer_arms, 1, __ATOMIC_RELAXED);
            }
        }
        __atomic_add_fetch(&fl_parks, 1, __ATOMIC_RELAXED);   /* before: "is parked" */
        prc = runloom_park_generic(1);
        if (prc < 0) {
            res = fl_poll(sema, deadline);
            goto out;
        }
    }
    while (__atomic_load_n(&sema->fiber_state, __ATOMIC_ACQUIRE) != FL_WOKEN)
        sched_yield();                              /* the waker is between two stores */
    res = Py_PARK_OK;
out:
    (void)fl_unlink(g);
    /* Unlinked, no signal mark can arrive; on Py_PARK_OK the wake hook is
     * done too.  On a timeout or an interrupt an unpark may still be under
     * way: the parking lot then calls wait() again, which ends here again. */
    __atomic_store_n(&g->fl_sig, 0, __ATOMIC_RELAXED);
    if (nhomes > 0) {
        int i;
        /* The wait is over: its timer entries must neither fire into the
         * fiber's next park nor sit in the heap until their deadlines. */
        __atomic_add_fetch(&g->timer_ticket, 1, __ATOMIC_ACQ_REL);
        for (i = 0; i < nhomes; i++)
            __atomic_add_fetch(&((runloom_sched_t *)homes[i])->timer_stale, 1,
                               __ATOMIC_ACQ_REL);
    }
    (void)__atomic_exchange_n(&g->wake_pending, 0, __ATOMIC_ACQ_REL);
    return res;
}

static void fl_wake(_PySemaphore *sema)
{
    runloom_g_t *g = (runloom_g_t *)sema->fiber;
    runloom_sched_t *L;
    __atomic_add_fetch(&fl_wakes, 1, __ATOMIC_RELAXED);
    __atomic_store_n(&sema->fiber_state, FL_WAKING, __ATOMIC_RELEASE);
    __atomic_add_fetch(&fl_inflight, 1, __ATOMIC_SEQ_CST);
    if (g->fl_hub &&
        (!__atomic_load_n(&fl_alive, __ATOMIC_SEQ_CST) ||
         g->fl_session != __atomic_load_n(&fl_session, __ATOMIC_ACQUIRE))) {
        /* Its M:N session is over: the hubs it would be queued on are going
         * or gone, and it will never run.  Its stack leaked with it, so the
         * entry is still valid. */
        __atomic_add_fetch(&fl_stale_wakes, 1, __ATOMIC_RELAXED);
        __atomic_store_n(&sema->fiber_state, FL_WOKEN, __ATOMIC_RELEASE);
        __atomic_sub_fetch(&fl_inflight, 1, __ATOMIC_RELEASE);
        return;
    }
    L = __atomic_load_n(&g->fl_list, __ATOMIC_ACQUIRE);
    if (L != NULL) {
        /* Held until WOKEN is stored: freeing the g (its stack, with the entry)
         * unlinks it under this lock first. */
        runloom_mutex_lock(&L->fl_lock);
        runloom_sched_wake_safe(g);
        __atomic_store_n(&sema->fiber_state, FL_WOKEN, __ATOMIC_RELEASE);
        runloom_mutex_unlock(&L->fl_lock);
    }
    else {
        /* Not linked yet: the waiter is between prepare and its wait, running,
         * so it cannot be freed; it will see WAKING and not park. */
        runloom_sched_wake_safe(g);
        __atomic_store_n(&sema->fiber_state, FL_WOKEN, __ATOMIC_RELEASE);
    }
    __atomic_sub_fetch(&fl_inflight, 1, __ATOMIC_RELEASE);
}

int runloom_fiberlock_install(void)
{
    fl_interp = PyInterpreterState_Get();
    _Py_fiber_park.prepare = fl_prepare;
    _Py_fiber_park.wait = fl_wait;
    _Py_fiber_park.wake = fl_wake;
    return 1;
}

void runloom_fiberlock_mn_start(void)
{
    __atomic_add_fetch(&fl_session, 1, __ATOMIC_RELEASE);
    __atomic_store_n(&fl_alive, 1, __ATOMIC_SEQ_CST);
}

void runloom_fiberlock_mn_stop(void)
{
    __atomic_store_n(&fl_alive, 0, __ATOMIC_SEQ_CST);
}

/* Dequeue the parking-lot entries of the M:N waiters on hub sched L (their
 * wakes are dropped now), so no release hands their locks to fibers that will
 * never run.  final: the hubs are joined, so every waiter still linked will
 * never run -- unlink it and give back its run-alive count. */
void runloom_fiberlock_mn_unpark(runloom_sched_t *L, int final)
{
    if (final) {
        /* Wakes that passed the fl_alive check before mn_fini cleared it may
         * still be touching their waiters' stacks: wait them out. */
        while (__atomic_load_n(&fl_inflight, __ATOMIC_SEQ_CST) != 0)
            sched_yield();
    }
    {
        int skip = 0;                       /* not final: the waiters stay linked */
        for (;;) {
            enum { BATCH = 64 };
            const void *addrs[BATCH];
            int n = 0, i, idx = 0, more = 0;
            runloom_g_t *g, *next;
            runloom_mutex_lock(&L->fl_lock);
            for (g = L->fl_head; g != NULL; g = next) {
                next = g->fl_next;
                if (!g->fl_hub) continue;
                if (!final && idx++ < skip) continue;
                if (n == BATCH) { more = 1; break; }
                addrs[n++] = g->fl_addr;
                if (final) fl_unlink_locked(g, L);
            }
            runloom_mutex_unlock(&L->fl_lock);
            for (i = 0; i < n; i++) {
                /* Wakes every waiter on the address: the dead ones' wake hooks
                 * drop it, a live thread re-checks the lock and waits again. */
                _PyParkingLot_UnparkAll(addrs[i]);
                if (final) runloom_foreign_park_release();   /* its park never ends */
            }
            if (!more) break;
            if (!final) skip += n;
        }
    }
}

void runloom_fiberlock_reset_after_fork(void)
{
    runloom_sched_t *me = runloom_sched_peek_here();
    long kept = 0, dead;
    runloom_g_t *g, *next;
    /* The child's only thread is the forking one, and CPython emptied the
     * parking lot (_PyParkingLot_AfterFork).  That thread's run(1) waiters
     * survive: wake them to retry the lock (their wait returns as if woken,
     * without a handoff, and the lock loop re-parks with a fresh entry).
     * Every other waiter (other threads', hubs') is dead. */
    __atomic_store_n(&fl_inflight, 0, __ATOMIC_RELAXED);
    __atomic_store_n(&fl_alive, 0, __ATOMIC_RELAXED);
    if (me != NULL) {
        runloom_mutex_init(&me->fl_lock);
        for (g = me->fl_head; g != NULL; g = next) {
            _PySemaphore *sema = (_PySemaphore *)g->fl_sema;
            next = g->fl_next;
            if (g->fl_hub) {
                fl_unlink_locked(g, me);
                __atomic_add_fetch(&fl_waiting, 1, __ATOMIC_RELAXED);  /* counted below */
                continue;
            }
            kept++;
            __atomic_store_n(&sema->fiber_state, FL_WOKEN, __ATOMIC_RELEASE);
            runloom_sched_wake_safe(g);
        }
    }
    dead = __atomic_load_n(&fl_waiting, __ATOMIC_RELAXED) - kept;
    for (; dead > 0; dead--)
        runloom_foreign_park_release();
    __atomic_store_n(&fl_waiting, kept, __ATOMIC_RELAXED);
}

int runloom_fiberlock_signal_wake(runloom_sched_t *s)
{
    runloom_g_t *g;
    int woke = 0;
    if (__atomic_load_n(&s->fl_head, __ATOMIC_RELAXED) == NULL)
        return 0;
    if (!runloom_iframe_main_signals_pending())
        return 0;
    runloom_mutex_lock(&s->fl_lock);
    for (g = s->fl_head; g != NULL; g = g->fl_next) {
        if (g->fl_hub || !g->fl_intr ||
            __atomic_load_n(&g->fl_sig, __ATOMIC_ACQUIRE))
            continue;
        __atomic_store_n(&g->fl_sig, 1, __ATOMIC_RELEASE);
        runloom_sched_wake_safe(g);          /* same thread: onto s's ready ring */
        __atomic_add_fetch(&fl_signal_wakes, 1, __ATOMIC_RELAXED);
        woke = 1;
        break;
    }
    runloom_mutex_unlock(&s->fl_lock);
    return woke;
}

unsigned long long runloom_fiberlock_parks_total(void)
{
    return __atomic_load_n(&fl_parks, __ATOMIC_RELAXED);
}

unsigned long long runloom_fiberlock_wakes_total(void)
{
    return __atomic_load_n(&fl_wakes, __ATOMIC_RELAXED);
}

unsigned long long runloom_fiberlock_stale_wakes_total(void)
{
    return __atomic_load_n(&fl_stale_wakes, __ATOMIC_RELAXED);
}

unsigned long long runloom_fiberlock_timer_arms_total(void)
{
    return __atomic_load_n(&fl_timer_arms, __ATOMIC_RELAXED);
}

unsigned long long runloom_fiberlock_signal_wakes_total(void)
{
    return __atomic_load_n(&fl_signal_wakes, __ATOMIC_RELAXED);
}

#else  /* no parking_lot.c hunk in this interpreter's headers */

int runloom_fiberlock_install(void) { return 0; }
void runloom_fiberlock_mn_start(void) { }
void runloom_fiberlock_mn_stop(void) { }
void runloom_fiberlock_mn_unpark(runloom_sched_t *L, int final) { (void)L; (void)final; }
void runloom_fiberlock_reset_after_fork(void) { }
void runloom_fiberlock_forget(runloom_g_t *g) { (void)g; }
int runloom_fiberlock_signal_wake(runloom_sched_t *s) { (void)s; return 0; }
unsigned long long runloom_fiberlock_parks_total(void) { return 0; }
unsigned long long runloom_fiberlock_wakes_total(void) { return 0; }
unsigned long long runloom_fiberlock_stale_wakes_total(void) { return 0; }
unsigned long long runloom_fiberlock_timer_arms_total(void) { return 0; }
unsigned long long runloom_fiberlock_signal_wakes_total(void) { return 0; }

#endif

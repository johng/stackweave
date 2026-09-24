/* Part of mn_sched.c -- #included into that translation unit, not compiled
 * standalone.  See mn_sched.c for the includes/typedefs/file-scope state
 * these definitions depend on. */

RUNLOOM_INLINE void runloom_mn_pending_inc(runloom_hub_t *h)
{
    /* spawn: bump THIS hub's pending only -- sharded, no global counter.
     * runloom_mn_run sums the per-hub fields.  A single global counter here was a
     * contended cache line that serialized parallel spawn (every spawn on every
     * core hit one line -> negative scaling).  RELEASE so the mn_run reader's
     * ACQUIRE sum observes the spawn before the g can run/complete.  No false 0
     * during a concurrent spawn: the SPAWNER is itself a live counted g (its
     * hub's pending >= 1), so the sum can't reach 0 while a spawn is in flight.
     * The work-stealing race is handled in runloom_mn_pending_steal. */
    __atomic_add_fetch(&h->pending, 1, __ATOMIC_RELEASE);
}

RUNLOOM_INLINE void runloom_mn_pending_complete(runloom_hub_t *h)
{
    /* complete: bump completed, drop THIS hub's pending.  No global counter --
     * runloom_mn_run sums the per-hub pending fields.  RELEASE on the dec so the
     * reader's ACQUIRE sum observes the completion (ordered after completed++). */
    __atomic_add_fetch(&h->sched.completed, 1, __ATOMIC_RELEASE);
    __atomic_sub_fetch(&h->pending, 1, __ATOMIC_RELEASE);
}

RUNLOOM_INLINE void runloom_mn_pending_steal(runloom_hub_t *victim,
                                              runloom_hub_t *thief)
{
    /* steal: work moves victim -> thief, total unchanged.  Bump the THIEF
     * BEFORE dropping the VICTIM so a concurrent runloom_mn_run sum sees the
     * stolen g transiently OVER-counted (on both hubs), NEVER under-counted (on
     * neither).  An over-count is harmless -- mn_run just polls again -- but a
     * transient sum of 0 would let run() exit while a stolen g is still live.
     * This reorder is what makes the sharded per-hub pending sum safe: the old
     * single global counter existed ONLY to dodge this steal race (see the note
     * at the steal call site in hub_main), and the reorder removes the need for
     * it.  ACQ_REL so the stolen g's writes are visible to the destination hub
     * before its later resume. */
    __atomic_add_fetch(&thief->pending,  1, __ATOMIC_ACQ_REL);
    __atomic_sub_fetch(&victim->pending, 1, __ATOMIC_ACQ_REL);
}

/* ---- Global stealable run-queue ----
 *
 * Default mode routes a woken g back to its ORIGIN hub's owner-drained
 * submission list (runloom_mn_hub_submit).  That strands the g whenever the
 * origin hub is stuck in a non-yielding blocking C call (the classic one:
 * libc getaddrinfo): the hub never loops, never drains its sub list, so the
 * woken g never reaches a stealable deque and no idle hub can rescue it.
 * This is asyncio's signature failure -- one blocking task stalling a whole
 * fan-out -- reproduced in runloom's narrow blocking-C-call case.
 *
 * A woken g carries its own migratable
 * PyThreadState, so it can resume on ANY hub.  We exploit that: wake_g
 * pushes the woken g onto this process-global queue instead of the origin
 * hub's sub list, and every idle hub drains it in hub_main's empty-local/
 * empty-deque path (alongside neighbour-steal).  An idle hub thus recovers a
 * stalled hub's woken work.  This is Go's per-P-local + global-runq model.
 *
 * LOCAL WAKE (the other half of Go's model).  A wake performed ON a general hub
 * thread does not come here at all: runloom_mn_woken_enqueue pushes the g onto
 * the waker's own deque (a lock-free owner push) and kicks one parked hub, so
 * the common cross-hub wake never takes this mutex.  This queue is the path for
 * a foreign (non-hub) waker, an offload-hub waker, a pinned g, controlled
 * replay, or a full deque.  A deque-resident woken g is QUEUED and carries its
 * queue ref exactly as an entry here does; hub_main detects it at the pick step
 * and runs the same claim + release.
 *
 * Accounting: the g stays counted in its origin hub's `pending` and the
 * global counter while it sits here (we do NOT rebalance per-hub pending on
 * pull).  That is correct because the idle path only ever reads the SUM of
 * per-hub pending, and the sum is conserved regardless of which hub holds the
 * count: spawn does +1 on one hub, completion does -1 on one hub, so the sum
 * always equals the global in-flight count.  runloom_mn_run reads the global
 * counter, which a queued g keeps non-zero until it actually completes.
 *
 * Single-owner safety (the load-bearing part).  ONE per-g state machine
 * (g->wake_state, see the field comment in runloom_sched.h) makes the MPMC queue
 * safe without per-entry refcounting and without the duplicate/double/lost-wake
 * hazards.  An earlier two-flag design (an exactly-once-wake dedup + a separate
 * exclusive-resume claim, with a re-push on claim contention) raced into a
 * livelock: the re-pushed entry survived the owner's next park (which re-armed
 * the wake flag), so a fresh wake made a SECOND entry, and the two entries
 * double-claimed and stranded the claim -> the re-push spun forever.  The state
 * machine removes BOTH the re-push and the possibility of two entries:
 *
 *   - wake_g CASes PARKED -> QUEUED and only the winner enqueues; a wake while
 *     the g is QUEUED, RUNNING, or RUNNING_WOKEN does NOT enqueue.  So a g holds
 *     AT MOST ONE entry, ever -- no stale duplicates, no spurious/double resume.
 *   - a hub CASes QUEUED -> RUNNING to claim the g it pulled.  Because the entry
 *     it pulled is the queue's SOLE reference to that g (at-most-one-entry) and
 *     only the entry's holder runs this CAS, the CAS cannot lose -> no re-push.
 *   - while RUNNING the g is owned by exactly one hub; a wake only flips RUNNING
 *     -> RUNNING_WOKEN (remembered, not enqueued), so no other hub can pull or
 *     attach the g's live tstate during the commit->detach window.  The owner,
 *     after detaching the tstate, ends the resume by CASing RUNNING -> PARKED,
 *     or, if a wake landed, RUNNING_WOKEN -> QUEUED and enqueues exactly once.
 *
 * "One entry per park" and "one resumer" are now the SAME invariant (QUEUED
 * holds the entry; RUNNING holds the owner; they are distinct states), which is
 * why no re-push is needed and no duplicate can form.  Each entry takes one
 * queue ref (incref at enqueue, decref at consume/drain); at most one entry
 * references a g, so the proven sub_list/deque single-ref lifetime model holds.
 *
 * MPMC: many producers (any thread's wake_g) and many consumers (idle hubs).
 * Mutex-protected singly-linked list through g->next -- the same field the
 * per-hub sub list uses, safe because a given g is in exactly one of {sub
 * list, global runq} at a time (a fresh g goes to its hub's sub list; a woken
 * g under per-g-tstate comes here, long after it left the sub list).
 *
 * Process-lifetime lock: a static initialiser (usable with no init/destroy,
 * survives mn_init/mn_fini cycles). */
static runloom_mutex_t  runloom_global_runq_lock = RUNLOOM_MUTEX_STATIC_INIT;
static runloom_g_t     *runloom_global_runq_head = NULL;
static runloom_g_t     *runloom_global_runq_tail = NULL;
static volatile long runloom_global_runq_len  = 0;
/* Entries with pin_hub1 == 0: the part of the queue any hub may pull. */
static volatile long runloom_global_runq_unpinned_len = 0;

/* Kick hub `h` out of whichever idle wait it is in -- the same two deliveries
 * runloom_mn_hub_submit makes for a sub_list push: the idle condvar (only when
 * the hub announced idle_waiting; the SEQ_CST fence in the caller pairs with
 * the hub's announce + re-check), and its netpoll pump self-pipe.  Both are
 * edge-safe: a kick that finds nobody waiting is a wasted syscall, never a
 * lost wake. */
static void runloom_mn_kick_hub_waits(runloom_hub_t *h)
{
    if (__atomic_load_n(&h->idle_waiting, __ATOMIC_ACQUIRE)) {
        RUNLOOM_RLOCK(&h->idle_lock, RUNLOOM_RANK_HUB_IDLE);
        runloom_cond_signal(&h->idle_cond);
        RUNLOOM_RUNLOCK(&h->idle_lock, RUNLOOM_RANK_HUB_IDLE);
    }
    runloom_netpoll_wake_pump((void *)h);
}

/* A global-runq push has no hub to hand its g to, so with the idle backoff off
 * (the default) nothing woke anyone and the g waited for the next 1 ms idle
 * pump of whichever hub pulled it: p99 380-600 us for a foreign-thread wake at
 * a sub-2 ms cadence, against ~30 us for the per-hub scheduler.  Kick ONE hub:
 * the pinned target, else the first general hub announcing an idle-condvar
 * wait, else the g's owner hub (it has this g pending, so it is in a timed pump
 * wait rather than a bare nap).  Not the hot path: local wakes never come here. */
static void runloom_mn_kick_for_runq_entry(runloom_g_t *g, int pin1)
{
    runloom_hub_t *h = NULL;
    int i;
    if (runloom_hubs == NULL) return;
    if (pin1 != 0) {
        if (pin1 - 1 < runloom_hub_count) h = &runloom_hubs[pin1 - 1];
    } else {
        for (i = 0; i < runloom_general_hub_count(); i++) {
            if (__atomic_load_n(&runloom_hubs[i].idle_waiting, __ATOMIC_ACQUIRE)) {
                h = &runloom_hubs[i];
                break;
            }
        }
        if (h == NULL && g->owner != NULL) {
            h = (runloom_hub_t *)((char *)g->owner - offsetof(runloom_hub_t, sched));
            if (h < runloom_hubs || h >= runloom_hubs + runloom_hub_count) h = NULL;
        }
    }
    if (h != NULL) runloom_mn_kick_hub_waits(h);
}

/* Link g onto the tail of the global run-queue.  Each entry holds one queue
 * ref (incref'd by the caller -- wake_g, or the RUNNING_WOKEN release) so it
 * keeps the g alive until a hub consumes it.  Enqueue is exactly-once: only the
 * winner of the PARKED->QUEUED (or RUNNING_WOKEN->QUEUED) CAS pushes, so a g is
 * on this queue at most once -- no re-push, no duplicate. */
static void runloom_mn_global_runq_push(runloom_g_t *g)
{
    int pin1;
    RUNLOOM_RLOCK(&runloom_global_runq_lock, RUNLOOM_RANK_GLOBAL_RUNQ);
    g->next = NULL;
    if (runloom_global_runq_tail != NULL) {
        runloom_global_runq_tail->next = g;
    } else {
        runloom_global_runq_head = g;
    }
    runloom_global_runq_tail = g;
    __atomic_add_fetch(&runloom_global_runq_len, 1, __ATOMIC_RELAXED);
    pin1 = __atomic_load_n(&g->pin_hub1, __ATOMIC_ACQUIRE);
    if (pin1 == 0) {
        __atomic_add_fetch(&runloom_global_runq_unpinned_len, 1, __ATOMIC_RELAXED);
    } else if (pin1 - 1 < runloom_hub_count) {
        __atomic_add_fetch(&runloom_hubs[pin1 - 1].runq_pinned_n, 1, __ATOMIC_RELAXED);
    }
    RUNLOOM_RUNLOCK(&runloom_global_runq_lock, RUNLOOM_RANK_GLOBAL_RUNQ);
    RUNLOOM_EVT(RUNLOOM_EVT_G_SUBMIT, g, NULL, 0);
    runloom_g_state_set(g, RUNLOOM_GST_SUBMITTED);
    /* WAKEP: a migratable woken g any idle hub may run -- kick a deep-parked hub
     * to re-scan + pull it (the global-runq counterpart of deque-surplus wakep).
     * The unlock above is the release publish; wakep_one's SEQ_CST fence orders
     * it before the idle_parked read, so no parked hub misses this push.
     * A PINNED entry must kick its own hub instead: every other hub walks
     * past it. */
    if (pin1 != 0) runloom_mn_wakep_pinned(pin1 - 1);
    else           runloom_mn_wakep_one();
    /* Dekker with the hub's idle announce (hub_main: idle_waiting=1, fence,
     * re-check the runq counters): the push above is published, so either the
     * hub sees the entry on its re-check and skips the wait, or it announced
     * first and this kick reaches it. */
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    runloom_mn_kick_for_runq_entry(g, pin1);
}

/* Pop one g for hub `hub_id` from the global run-queue (NULL if it holds
 * nothing this hub may run).  No per-hub pending rebalance (see header: only
 * the sum matters, and the mutex release/acquire already orders the g's writes
 * for the puller).
 */
static runloom_g_t *runloom_mn_global_runq_pull(int hub_id)
{
    runloom_g_t *g, *prev;
    const int want = hub_id + 1;   /* +1 encoding; hub_id<0 -> match unpinned only */
    /* Exclusion 2 from the puller's side: an OFFLOAD hub takes only what is
     * pinned to it.  Taking an unpinned general fiber would strand it behind
     * the hub's next blocking call (it was the one general-work path left
     * onto an offload hub). */
    const int take_unpinned = !(hub_id >= 0 && runloom_hub_is_offload(hub_id));
    /* Lock-free fast-out: the queue is empty in the common case (default
     * mode never touches it; per-g-tstate only when a wake is in flight),
     * so skip the mutex when there is clearly nothing to take.  A racing
     * push between this load and a lock would just be picked up next loop. */
    if (__atomic_load_n(&runloom_global_runq_len, __ATOMIC_ACQUIRE) == 0) {
        return NULL;
    }
    RUNLOOM_RLOCK(&runloom_global_runq_lock, RUNLOOM_RANK_GLOBAL_RUNQ);
    prev = NULL;
    g = runloom_global_runq_head;
    while (g != NULL) {
        /* First match, not head-only: a head entry pinned to a busy hub would
         * otherwise block every unpinned entry behind it.  One load of
         * pin_hub1 per entry; its stores are under this lock (repin) or
         * pre-publication (spawn). */
        int p1 = __atomic_load_n(&g->pin_hub1, __ATOMIC_ACQUIRE);
        if ((p1 == 0 && take_unpinned) || p1 == want) break;
        prev = g;
        g = g->next;
    }
    if (g != NULL) {
        /* Unlink g from wherever the walk stopped.  Read g->next for the tail
         * fixup BEFORE clearing it -- clearing first would strand the tail
         * pointer at a node that is no longer last. */
        if (prev == NULL) runloom_global_runq_head = g->next;
        else              prev->next = g->next;
        if (g->next == NULL) runloom_global_runq_tail = prev;
        g->next = NULL;
        __atomic_sub_fetch(&runloom_global_runq_len, 1, __ATOMIC_RELAXED);
        {   /* Mirror the push-side accounting exactly, or the idle scan drifts
             * and a hub either spins forever or sleeps through its own work. */
            int pin1 = __atomic_load_n(&g->pin_hub1, __ATOMIC_ACQUIRE);
            if (pin1 == 0) {
                __atomic_sub_fetch(&runloom_global_runq_unpinned_len, 1,
                                   __ATOMIC_RELAXED);
            } else if (pin1 - 1 < runloom_hub_count) {
                __atomic_sub_fetch(&runloom_hubs[pin1 - 1].runq_pinned_n, 1,
                                   __ATOMIC_RELAXED);
            }
        }
    }
    RUNLOOM_RUNLOCK(&runloom_global_runq_lock, RUNLOOM_RANK_GLOBAL_RUNQ);
    return g;
}

/* The only post-spawn writer of g->pin_hub1.  push and pull classify an entry
 * by pin_hub1 UNDER THIS LOCK, so a bare store on a queued g would make pull
 * decrement a different counter than push incremented, and the drift never
 * heals (a stuck count keeps a hub from ever sleeping, or lets it sleep through
 * work it could take).  The O(n) walk is fine: only pin() reaches here. */
static void runloom_mn_global_runq_repin(runloom_g_t *g, int new_pin1)
{
    runloom_g_t *it;
    int queued = 0, old_pin1;
    RUNLOOM_RLOCK(&runloom_global_runq_lock, RUNLOOM_RANK_GLOBAL_RUNQ);
    old_pin1 = __atomic_load_n(&g->pin_hub1, __ATOMIC_ACQUIRE);
    for (it = runloom_global_runq_head; it != NULL; it = it->next) {
        if (it == g) { queued = 1; break; }
    }
    if (queued && old_pin1 != new_pin1 && runloom_hubs != NULL) {
        if (old_pin1 == 0) {
            __atomic_sub_fetch(&runloom_global_runq_unpinned_len, 1, __ATOMIC_RELAXED);
        } else if (old_pin1 - 1 < runloom_hub_count) {
            __atomic_sub_fetch(&runloom_hubs[old_pin1 - 1].runq_pinned_n, 1,
                               __ATOMIC_RELAXED);
        }
        if (new_pin1 == 0) {
            __atomic_add_fetch(&runloom_global_runq_unpinned_len, 1, __ATOMIC_RELAXED);
        } else if (new_pin1 - 1 < runloom_hub_count) {
            __atomic_add_fetch(&runloom_hubs[new_pin1 - 1].runq_pinned_n, 1,
                               __ATOMIC_RELAXED);
        }
    }
    __atomic_store_n(&g->pin_hub1, new_pin1, __ATOMIC_RELEASE);
    RUNLOOM_RUNLOCK(&runloom_global_runq_lock, RUNLOOM_RANK_GLOBAL_RUNQ);
    /* Outside the runq lock (wakep takes hub idle_lock -- do not invert ranks).
     * A re-pinned queued entry changes WHICH hub may take it, so kick the new
     * owner: it may be deep-parked, and after the re-pin nobody else will. */
    if (queued) {
        if (new_pin1 != 0) {
            runloom_mn_wakep_pinned(new_pin1 - 1);
        } else {
            runloom_mn_wakep_one();
        }
    }
}

/* Cross-hub migration is always on: every fiber owns its own PyThreadState, so
 * a woken g is routed to the global stealable run-queue and resumes on ANY idle
 * hub.  The mode flag (runloom_get_per_g_tstate_mode) is set by mn_init and
 * cleared by mn_fini, so it doubles as "an M:N runtime is live": code shared
 * with the single-thread scheduler still reads it to pick its path.
 *
 * Soundness needs CPython built with BOTH optional patches in src/patches/ --
 * alloc-home (Py_TSTATE_ALLOC_HOME: a per-g tstate allocates on the hub now
 * running it, so no mimalloc heap migrates OS threads) and exec-home
 * (Py_TSTATE_EXEC_HOME: _PyThreadState_GET()/_Py_ThreadId() stay uncacheable
 * across a park/resume).  Nothing checks for them; on a stock interpreter the
 * scheduler migrates anyway and can crash under churn at H>=2. */
static int runloom_use_global_runq(void)
{
    return runloom_get_per_g_tstate_mode();
}

/* ---- sysmon watchdog (stalled-hub detection) ----
 *
 * One extra OS thread, always on for an M:N run, periodically scans every hub's
 * resume_start_ns and flags a hub stuck inside a single runloom_coro_resume
 * longer than STACKWEAVE_SYSMON_MS (the unanticipated-block signature -- a fiber
 * occupying its hub's OS thread with a non-yielding blocking C call that
 * offload never wrapped).  It only observes: a wedged hub's woken fibers are
 * already recovered by the global run-queue, which any idle hub drains.  The
 * deadlock census reads the same per-resume instrumentation.
 *
 * PyThreadState->state values (cpython/pystate.h documents the field;
 * the numeric values live in the internal pycore_pystate.h).  Mirrored here
 * so the watchdog can classify a wedge WITHOUT the Python C-API: it reads
 * h->tstate->state to tell a DETACHED stall (the running g released the tstate
 * via Py_BEGIN_ALLOW_THREADS -> a well-behaved blocking-IO call) from an
 * ATTACHED one (CPU-bound bytecode or a raw tstate-holding syscall) and from a
 * SUSPENDED one (a stop-the-world is in progress). */
#define RUNLOOM_TS_DETACHED  0
#define RUNLOOM_TS_ATTACHED  1
#define RUNLOOM_TS_SUSPENDED 2

/* Read a tstate's free-threaded attach state (DETACHED/ATTACHED/SUSPENDED). */
RUNLOOM_INLINE int runloom_tstate_attach_state(PyThreadState *ts)
{
    if (ts == NULL) return -1;
    return __atomic_load_n(&ts->state, __ATOMIC_ACQUIRE);
}

static int       runloom_sysmon_quiet = 0;   /* STACKWEAVE_SYSMON_QUIET: suppress
                                              * the per-wedge WEDGED/RECOVERED
                                              * diagnostic spam */
static long long runloom_sysmon_wedge_ns = 50LL * 1000000LL;   /* 50 ms default */
/* ATTACHED-wedge preempt threshold (the CPU time-slice); defaults to the wedge
 * budget, STACKWEAVE_PREEMPT_MS tunes it independently. */
static long long runloom_sysmon_preempt_ns = 50LL * 1000000LL;
/* Preemption has no opt-out.  The one writer besides init is the fatal-signal
 * crash handler (runloom_sched_freeze_for_crash), which clears it so the
 * faulting g stays put while the handler chains out. */
static int       runloom_preempt_enabled = 1;
static long long runloom_sysmon_tick_ns  = 10LL * 1000000LL;   /* scan every 10 ms (base) */
static long long runloom_sysmon_tick_base_ns = 10LL * 1000000LL; /* unbacked-off tick */
static runloom_thread_t runloom_sysmon_thread;
static int       runloom_sysmon_running = 0;   /* 1 once the thread is spawned */
static volatile int runloom_sysmon_stop = 0;


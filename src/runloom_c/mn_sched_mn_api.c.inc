/* Part of mn_sched.c -- #included into that translation unit, not compiled
 * standalone.  See mn_sched.c for the includes/typedefs/file-scope state
 * these definitions depend on. */


int runloom_mn_hub_count(void)
{
    return runloom_hub_count;
}

/* How many of runloom_hub_count are RESERVED OFFLOAD hubs (the array tail).
 * 0 means the feature is off and every hub is general.  Callers use this to
 * decide whether offload_fiber is available before reaching for it. */
int runloom_mn_offload_hub_count(void)
{
    return __atomic_load_n(&runloom_offload_hub_count, __ATOMIC_RELAXED);
}

/* R0 gauges: per-hub census over runloom_hubs[].  Lock-free, following the
 * sysmon walk pattern (mn_sched_sysmon.c.inc): the array is PyMem_Calloc'd
 * once at mn_init and never realloc'd within a run, so snapshotting
 * runloom_hub_count and reading each hub's atomic field is race-free.  Safe
 * from m_stats -- takes no lock any pump/hub holds.
 *
 * mn_pending_total: submitted-minus-completed gs across all hub run queues
 * (the true live in-scheduler g population; work-stealing moves `pending`
 * between hubs but the SUM is conserved).  Falls to ~0 at quiescence; a
 * climbing floor across soak iterations = fibers stuck in hub queues.
 * mn_completed_total: cumulative retired gs across hubs -- an odometer
 * (never falls), a liveness signal to confirm the soak is retiring work.
 * mn_deque_depth_total: stealable gs across all hub deques -- fresh ones, plus
 * locally-woken ones under migration (runloom_mn_woken_enqueue). */
long runloom_mn_pending_total(void)
{
    long total = 0;
    int i, n = runloom_hub_count;
    if (runloom_hubs == NULL) return 0;
    for (i = 0; i < n; i++)
        total += __atomic_load_n(&runloom_hubs[i].pending, __ATOMIC_ACQUIRE);
    return total;
}

long long runloom_mn_completed_total(void)
{
    long long total = 0;
    int i, n = runloom_hub_count;
    if (runloom_hubs == NULL) return 0;
    for (i = 0; i < n; i++)
        total += (long long)__atomic_load_n(&runloom_hubs[i].sched.completed,
                                            __ATOMIC_ACQUIRE);
    return total;
}

long runloom_mn_deque_depth_total(void)
{
    long total = 0;
    int i, n = runloom_hub_count;
    if (runloom_hubs == NULL) return 0;
    for (i = 0; i < n; i++) {
        long d = runloom_cldeque_size(&runloom_hubs[i].deque);
        if (d > 0) total += d;
    }
    return total;
}

void *runloom_mn_current_hub_opaque(void)
{
    return (void *)runloom_tls_hub;
}

int runloom_mn_hub_id_of(void *hub_opaque)
{
    if (hub_opaque == NULL) return -1;
    return ((runloom_hub_t *)hub_opaque)->id;
}

runloom_g_t *runloom_mn_tls_current_g(void)
{
    return runloom_tls_current_g;
}

void runloom_mn_tls_mark_parked(void)
{
    runloom_tls_self_queued = 1;
    runloom_tls_parked_offqueue = 1;
}

void runloom_mn_tls_mark_parked_foreign(void)
{
    /* The segment about to yield is parking on a foreign-thread completion
     * (blocking-IO / offload).  Recorded per hub-thread; consumed at the resume
     * boundary by runloom_mn_ctrl_release.  No effect unless the controlled
     * barrier census is active. */
    runloom_tls_parked_foreign = 1;
}

runloom_sched_t *runloom_mn_current_sched(void)
{
    runloom_hub_t *h = runloom_tls_hub;
    return h ? &h->sched : NULL;
}

/* Push g onto a hub's submission list.  Called by netpoll pump to
 * route an I/O-woken g back to whichever hub it was running on.
 * (Also used internally by mn_fiber.)  Hub_main drains submissions every
 * iteration and routes each entry to either the deque (if g is fresh)
 * or the local FIFO (if g has saved state -- the netpoll-wake case).
 *
 * Idempotency: a CAS on g->in_sub_queue makes duplicate submissions
 * no-ops.  Under M:N + free-threaded 3.13t a parker can legitimately
 * be wake_g'd more than once (e.g., wake_g from a netpoll pump that
 * unlinked it normally, followed by a stale wake from the safety
 * unlink at the next wait_fd's yield-return).  Without the CAS the
 * sub queue ends up with g twice, hub_main pops it twice, and the
 * second resume hits a g whose coro was already destroyed by the
 * decref that ran after the first run-to-completion -- segfault in
 * runloom_asm_swap on *(NULL coro). */
static void runloom_mn_hub_submit(runloom_hub_t *h, runloom_g_t *g)
{
    int expected = 0;
    int per_g = runloom_get_per_g_tstate_mode();
    /* QUEUE REF, taken via TRY_INCREF *before* touching g->in_sub_queue.
     *
     * A queued g used to be held only by its spawn ref, so a STALE wake_g racing
     * the g's completion (the duplicate-wake the comment above describes) could
     * re-reference a g that completion had already freed -> use-after-free (the
     * arm64 per-hub-kqueue crash: a freed g, magic 0xDEAD, re-submitted into a
     * hub's sub_head and resumed).
     *
     * The ref MUST be a refcount>0-gated try_incref taken up front, not a plain
     * incref after the CAS: CBMC (tools/verify/cbmc/sched_qref_cbmc.c) shows the latter
     * still has a window where completion frees the g between the CAS and the
     * incref (it resurrects a freed g).  try_incref FAILS if the g is already
     * freeing (refcount 0), so a stale wake on a completed g bails here, and on
     * success the g is pinned for the lifetime of THIS queue entry -- hub_main
     * drops it exactly once when it consumes the entry (the in_sub_queue 1->0
     * exchange).  Per-g-tstate mode uses the global-runq from_runq ref instead,
     * so it keeps the original path. */
    if (!per_g && !runloom_g_try_incref(g))
        return;                          /* g is freeing -> drop the stale wake */
    /* Perturbation seam (RUNLOOM_DELAY): widen the window between the
     * try_incref that guards resurrection and the in_sub_queue CAS below -- the
     * exact ABA window sched_qref_cbmc.c proves.  The formal proof shows it is
     * safe FOR ALL interleavings; this widener lets a soak/TSan run actually
     * HIT that window on the real .so.  No-op unless RUNLOOM_DELAY names it. */
    runloom_delay_inject(RUNLOOM_DLY_G_RESURRECT);
    /* Defence-in-depth (debug): a live g must not already be DONE/FREED. */
    RUNLOOM_G_ASSERT_NOT(g, RUNLOOM_GST_MASK_DEAD);
    if (!__atomic_compare_exchange_n(&g->in_sub_queue, &expected, 1,
                                     0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        /* Already queued -> drop the ref we just took and bail.  The pending
         * resume picks up whatever state the g has when hub_main reaches it. */
        if (!per_g) runloom_g_decref(g);
        return;
    }
    /* Controlled-replay deferred publish: a cross-hub submit made DURING a
     * running segment (the spawning hub holds the baton and is running a g) is
     * staged on the SPAWNING hub per target, then flushed onto the target's
     * sub_list atomically at this segment's release (runloom_mn_ctrl_stage_flush in
     * resume_end).  That makes the target's drain observe the complete set the
     * segment produced rather than a partial mid-segment snapshot -- the
     * deterministic-replay fix.  Gated tight so it is inert otherwise: barrier
     * active, the caller is a hub (runloom_tls_hub) running a fiber
     * (runloom_tls_current_g) targeting a DIFFERENT hub, and staging is allocated.
     * Same-hub submits, non-segment producers (idle pump / pre-run placement),
     * and the entire default scheduler fall straight through to the immediate
     * push below (zero added cost there). */
    {
        runloom_hub_t *cur = runloom_tls_hub;
        if (runloom_mn_ctrl.enabled && runloom_mn_ctrl.barrier &&
            cur != NULL && cur != h && cur->stage_head != NULL &&
            runloom_tls_current_g != NULL &&
            h->id >= 0 && h->id < runloom_hub_count) {
            RUNLOOM_NEXT_SET(g, NULL, 5);   /* staging (controlled mode) */
            if (cur->stage_tail[h->id] != NULL)
                RUNLOOM_NEXT_SET(cur->stage_tail[h->id], g, 5);
            else
                cur->stage_head[h->id] = g;
            cur->stage_tail[h->id] = g;
            cur->stage_pending++;
            RUNLOOM_EVT(RUNLOOM_EVT_G_SUBMIT, g, h, 0);
            runloom_g_state_set(g, RUNLOOM_GST_SUBMITTED);
            return;
        }
    }
    RUNLOOM_RLOCK(&h->sub_lock, RUNLOOM_RANK_HUB_SUB);
    RUNLOOM_NEXT_SET(g, NULL, 1);   /* hub_submit: new tail's next */
    if (h->sub_tail != NULL) {
        RUNLOOM_NEXT_SET(h->sub_tail, g, 1);   /* link old tail -> g */
    } else {
        /* RELEASE: publishes g to the lock-free deadlock census's ACQUIRE read of
         * sub_head (mn_has_wakeable_work) -- tools/README Finding D #4. */
        __atomic_store_n(&h->sub_head, g, __ATOMIC_RELEASE);
    }
    h->sub_tail = g;
    RUNLOOM_RUNLOCK(&h->sub_lock, RUNLOOM_RANK_HUB_SUB);
    RUNLOOM_EVT(RUNLOOM_EVT_G_SUBMIT, g, h, 0);
    runloom_g_state_set(g, RUNLOOM_GST_SUBMITTED);
    /* MNWAKE-TRACE: the DURABLE append to the owner hub's sub_head (model
     * WakerAppend) -- opens a model episode for g.  Gated on g->snap.valid so only
     * a WOKEN g (snapped its state at park) emits, not a FRESH spawn (snap-invalid,
     * routed to the deque not the ready FIFO -> no HUB_DRAIN, not a wake episode);
     * this matches the HUB_DRAIN emit (also snap.valid only).  snap.valid is set at
     * park on the owner thread before this foreign waker's parked_safe CAS, so the
     * read is race-free.  Immediate-push path only (the staged path returns above). */
    /* trace_active() short-circuits so g->snap.valid is NOT read in production
     * (trace off) -- removing the benign diagnostics-only race against the owner
     * hub's snap.valid write at park/resume, plus a hot-path load. */
    if (runloom_mnwake_trace_active() && g->snap.valid)
        runloom_mnwake_trace_event("FOREIGN_WAKE", (unsigned long)(uintptr_t)g, 0);
    /* BUG #10 throughput: bump the submit generation, then signal the target hub
     * if it is parked in its idle condvar (it owns parked gs and nothing else to
     * run), so the woken g is drained in ~us instead of stranded for idle_ns
     * (~100us).  RELEASE bump pairs with the waiter's predicate re-read.  The
     * idle_waiting hint is a lock-free fast path: when the hub is NOT waiting
     * (the common busy case) we skip the lock entirely.  A signal that races the
     * hub entering/leaving the wait is harmless -- the wait is TIMED, so a
     * (rare) missed signal just falls back to the idle_ns poll, never a hang. */
    __atomic_add_fetch(&h->sub_gen, 1, __ATOMIC_RELEASE);
    if (__atomic_load_n(&h->idle_waiting, __ATOMIC_ACQUIRE)) {
        RUNLOOM_RLOCK(&h->idle_lock, RUNLOOM_RANK_HUB_IDLE);
        runloom_cond_signal(&h->idle_cond);
        RUNLOOM_RUNLOCK(&h->idle_lock, RUNLOOM_RANK_HUB_IDLE);
    }
    /* Cross-hub wake latency fix: the idle_cond signal above ONLY reaches a hub
     * parked in the parked==0 idle-condvar branch.  But `parked` is the GLOBAL
     * netpoll-parked count, so whenever ANY fiber anywhere is parked on I/O
     * (a server, file I/O, a timer, even the harness progress loop), every idle
     * hub instead blocks in netpoll_pump -> epoll_wait(idle_ns) and the cond
     * signal hits nobody.  The submitted g then waits up to the full pump
     * timeout (~1ms) before the hub loops back to drain sub_head -- which pins
     * cross-hub lock/channel handoff throughput to ~700/s (big_100 p87/p43/p47).
     * Kick the shared pump-wake eventfd (registered in the epoll set) so a
     * pumping hub returns from epoll_wait NOW; it drains the eventfd and re-loops
     * to pick up sub_head/global-runq work.  Level-triggered + non-exclusive, so
     * Per-hub kqueue: kick ONLY the target hub `h`'s own pump (its self-pipe),
     * not a broadcast -- this is the hot cross-hub handoff path.  Shared-handle
     * backends (epoll/select) ignore the arg. */
    runloom_netpoll_wake_pump((void *)h);
    /* MNWAKE-TRACE: both kicks fired (model WakerSignal -- idle_cond + wake-pump
     * eventfd, each FREE delivery).  One event per WOKEN-g submit (snap.valid, as
     * for FOREIGN_WAKE); delivery left free. */
    if (runloom_mnwake_trace_active() && g->snap.valid)
        runloom_mnwake_trace_event("SIGNAL", (unsigned long)(uintptr_t)g, 0);
}

/* Confine g's next (and every later) wake to hub `hub_id`.  See the contract in
 * mn_sched.h and the pin_hub1 field comment in runloom_sched.h. */
int runloom_mn_pin_for_wake(runloom_g_t *g, int hub_id)
{
    if (g == NULL) {
        PyErr_SetString(PyExc_RuntimeError, "pin(): dead fiber handle");
        return -1;
    }
    if (runloom_hubs == NULL || runloom_hub_count <= 0) {
        PyErr_SetString(PyExc_RuntimeError,
                        "pin(): the M:N runtime is not running");
        return -1;
    }
    if (hub_id >= runloom_hub_count) {
        PyErr_Format(PyExc_ValueError,
                     "pin(%d): only %d hub%s running (0..%d)",
                     hub_id, runloom_hub_count,
                     runloom_hub_count == 1 ? " is" : "s are",
                     runloom_hub_count - 1);
        return -1;
    }

    if (hub_id < 0) {                    /* clear the pin */
        runloom_mn_global_runq_repin(g, 0);
        return 0;
    }

    runloom_mn_global_runq_repin(g, hub_id + 1);
    return 0;
}

/* Enqueue a woken migratable g (wake_state QUEUED, holding its queue ref) where
 * a hub will find it soonest.
 *
 * GO-STYLE LOCAL WAKE.  When the waker is itself a GENERAL hub thread, the g
 * goes onto the waker's OWN Chase-Lev deque: a legal owner push (same OS
 * thread, no lock), and the data the waker just touched is what the g reads
 * next.  The waker's hub picks it up at its next pick step; any idle general
 * hub may steal it meanwhile (wakep_one kicks one).  The global run-queue then
 * serves only the wakes with nowhere local to go:
 *   - a FOREIGN (non-hub) waker -- main-thread signal delivery, the asyncio
 *     bridge, an mp feeder thread -- has no deque of its own;
 *   - an OFFLOAD hub as waker: general work must never sit on an offload hub
 *     (RUNLOOM_OFFLOAD_HUBS exclusions 1+2) and general hubs never steal from
 *     one, so it would strand behind the hub's next blocking call;
 *   - a PINNED g: a deque is stealable by ANY general hub, so only the global
 *     queue (whose pull honours pin_hub1) can confine it;
 *   - controlled replay (no-steal): a deque-resident woken g would wait for
 *     the waker's fiber to yield, a schedule the replay model does not have;
 *   - the waker's deque is full.
 * The wake state machine is untouched: the caller took the PARKED->QUEUED (or
 * *_WOKEN->QUEUED) edge and the queue ref exactly as for the global path, and
 * hub_main recognises a QUEUED g popped/stolen from a deque and runs the same
 * QUEUED->RUNNING claim + queue-ref drop it runs for a global pull.  Per-hub
 * `pending` is NOT rebalanced, as for the global path: under migration only the
 * SUM is meaningful (spawn +1 on the placement hub, completion -1 on whichever
 * hub finishes; see the runq header). */
static void runloom_mn_woken_enqueue(runloom_g_t *g)
{
    runloom_hub_t *cur = runloom_tls_hub;
    if (cur != NULL && runloom_hubs != NULL &&
        !runloom_hub_is_offload(cur->id) &&
        !runloom_mn_ctrl_no_steal() &&
        __atomic_load_n(&g->pin_hub1, __ATOMIC_ACQUIRE) == 0 &&
        runloom_cldeque_push(&cur->deque, g) == 0) {
        RUNLOOM_COVER_HIT(RUNLOOM_COV_LOCAL_WAKE);
        RUNLOOM_EVT(RUNLOOM_EVT_G_SUBMIT, g, cur, 0);
        runloom_g_state_set(g, RUNLOOM_GST_SUBMITTED);
        /* The push's RELEASE on `bottom` publishes the g; wakep_one's SEQ_CST
         * fence orders it before the idle_parked read -- the Dekker pair with
         * runloom_mn_park_enter, whose re-scan under migration counts a deque
         * of ONE as surplus (runloom_mn_any_stealable_work), so a hub that is
         * parking right now either sees this g or gets kicked. */
        runloom_mn_wakep_one();
    } else {
        runloom_mn_global_runq_push(g);
    }
    /* Same cross-hub wake-latency fix as hub_submit: an idle hub blocked in
     * epoll_wait re-loops to steal / pull.  No-op if the pump is not armed. */
    runloom_netpoll_wake_pump(NULL);
}

void runloom_mn_wake_g(void *hub_opaque, runloom_g_t *g)
{
    if (hub_opaque == NULL) {
        /* g belongs to a per-thread (single-thread) scheduler, or netpoll was
         * used outside any hub context.  Route via runloom_sched_wake, which (Phase
         * 2) sends the g to its OWNER sched's wake_list + kicks that thread's
         * pump when the waker is a foreign thread -- e.g. the shared netpoll
         * pump on one loop's thread delivering an fd event for a g parked on
         * another loop's thread.  A plain ready_push here would wake it on the
         * wrong (waker's) thread. */
        runloom_sched_wake(g);
        return;
    }
    if (runloom_use_global_runq()) {
        /* per-g-tstate OR steal-woken: g is migratable, so route it where a
         * hub other than its origin can reach it -- the waker's own deque, else
         * the global run-queue (runloom_mn_woken_enqueue) -- instead of the
         * origin hub's owner-drained sub list; recovers it even if the origin
         * hub is wedged in a blocking C call.  Drive the per-g wake state machine (see
         * runloom_sched.h); it is tstate-agnostic -- the snap (steal-woken) or the
         * per-g tstate (per-g-tstate) carries the migrated state:
         *
         *   PARKED  -> QUEUED          : we won the wake; enqueue exactly once
         *                                (+ a queue ref so the entry keeps g
         *                                alive until a hub consumes it).
         *   RUNNING -> RUNNING_WOKEN    : g is owned by a hub mid-resume; just
         *                                remember the wake -- the owner enqueues
         *                                it at release.  Enqueuing now would let
         *                                a second hub attach g's live tstate
         *                                during the owner's commit->detach gap.
         *   QUEUED / RUNNING_WOKEN      : a wake is already pending -> drop, so a
         *                                g is never enqueued twice (the old
         *                                two-flag design's livelock).
         *
         * CAS-on-failure reloads `st`, so the loop only spins while it keeps
         * losing the PARKED->QUEUED or RUNNING->RUNNING_WOKEN race (a couple of
         * instructions); every other state returns immediately. */
        int st = __atomic_load_n(&g->wake_state, __ATOMIC_ACQUIRE);
        (void)hub_opaque;   /* origin no longer needed: no per-hub rebalance */
        RUNLOOM_G_ASSERT_NOT(g, RUNLOOM_GST_MASK_DEAD);
        for (;;) {
            if (st == RUNLOOM_WS_PARKED) {
                if (__atomic_compare_exchange_n(&g->wake_state, &st,
                                                RUNLOOM_WS_QUEUED, 0,
                                                __ATOMIC_ACQ_REL,
                                                __ATOMIC_ACQUIRE)) {
                    RUNLOOM_WS_NOTE(RUNLOOM_WS_PARKED, RUNLOOM_WS_QUEUED);
                    runloom_g_incref(g);   /* queue ref, dropped when consumed */
                    runloom_mn_woken_enqueue(g);
                    return;
                }
                /* lost: st reloaded to the current state; re-evaluate */
            } else if (st == RUNLOOM_WS_RUNNING) {
                if (__atomic_compare_exchange_n(&g->wake_state, &st,
                                                RUNLOOM_WS_RUNNING_WOKEN, 0,
                                                __ATOMIC_ACQ_REL,
                                                __ATOMIC_ACQUIRE)) {
                    RUNLOOM_WS_NOTE(RUNLOOM_WS_RUNNING, RUNLOOM_WS_RUNNING_WOKEN);
                    return;   /* owner will enqueue at release */
                }
            } else if (st == RUNLOOM_WS_SWEEPING) {
                if (__atomic_compare_exchange_n(&g->wake_state, &st,
                                                RUNLOOM_WS_SWEEPING_WOKEN, 0,
                                                __ATOMIC_ACQ_REL,
                                                __ATOMIC_ACQUIRE)) {
                    RUNLOOM_WS_NOTE(RUNLOOM_WS_SWEEPING, RUNLOOM_WS_SWEEPING_WOKEN);
                    return;   /* sweeper will enqueue at release */
                }
            } else {
                return;   /* QUEUED / RUNNING_WOKEN / SWEEPING_WOKEN: pending */
            }
        }
    }
    runloom_mn_hub_submit((runloom_hub_t *)hub_opaque, g);
}

/* Idle-stack-sweep handshake.  An idle hub about to
 * MADV_DONTNEED a long-parked g's below-SP stack pages must hold the g
 * un-resumable for the madvise's duration, or another hub could pull a wake and
 * resume the g into pages the kernel is concurrently zeroing.  try_claim CASes
 * PARKED -> SWEEPING (the same exclusivity QUEUED->RUNNING gives a resumer); it
 * loses to any non-PARKED state (woken/owned) and the sweeper skips that g.
 * The held g cannot run, complete, or be freed, so its pointer stays valid
 * across the unlocked madvise -- restoring the exact liveness the default
 * (sole-resumer) mode relies on. */
int runloom_mn_sweep_try_claim(runloom_g_t *g)
{
    int st = RUNLOOM_WS_PARKED;
    if (__atomic_compare_exchange_n(&g->wake_state, &st, RUNLOOM_WS_SWEEPING,
                                    0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        RUNLOOM_WS_NOTE(RUNLOOM_WS_PARKED, RUNLOOM_WS_SWEEPING);
        return 1;
    }
    return 0;
}

/* End a sweep claim after the madvise.  SWEEPING -> PARKED if no wake landed.
 * If a wake landed during the madvise the state is SWEEPING_WOKEN (wake_g
 * remembered it without enqueueing); convert it to QUEUED and push exactly once
 * -- the sweeper is the sole "winner" of that deferred wake, mirroring the
 * RUNNING_WOKEN -> QUEUED release, so the wake is never lost and never
 * duplicated. */
void runloom_mn_sweep_claim_release(runloom_g_t *g)
{
    int st = RUNLOOM_WS_SWEEPING;
    if (__atomic_compare_exchange_n(&g->wake_state, &st, RUNLOOM_WS_PARKED, 0,
                                    __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        RUNLOOM_WS_NOTE(RUNLOOM_WS_SWEEPING, RUNLOOM_WS_PARKED);
        return;   /* clean release; no wake during the madvise */
    }
    /* The only state try_claim could leave that isn't SWEEPING is
     * SWEEPING_WOKEN (no other actor touches a SWEEPING g except wake_g, which
     * only flips it to SWEEPING_WOKEN).  Re-enqueue the deferred wake. */
    RUNLOOM_WS_NOTE(RUNLOOM_WS_SWEEPING_WOKEN, RUNLOOM_WS_QUEUED);
    __atomic_store_n(&g->wake_state, RUNLOOM_WS_QUEUED, __ATOMIC_RELEASE);
    runloom_g_incref(g);   /* queue ref, dropped when consumed */
    runloom_mn_woken_enqueue(g);
}

int runloom_mn_yield_current(void)
{
    runloom_hub_t *h = runloom_tls_hub;
    runloom_g_t *g = runloom_tls_current_g;
    if (h == NULL || g == NULL) {
        return 0;
    }
    /* Trivial-switch fast path: if there's no other work for this hub
     * -- no yielded g in local FIFO, no fresh g in our deque, no
     * sleeper due, no parked I/O -- yielding would just snap, swap to
     * hub_main, find an empty queue, swap back.  Skip it.
     *
     * We do NOT peek neighbours' deques here: a steal happens only when
     * a hub goes idle (hub_main's main path), and idle implies the
     * neighbour itself has nothing local to run.  Letting a g monopolise
     * a hub while neighbours have work is fine -- the work-stealing
     * scheduler is allowed to leave stealable items on a busy hub.
     * This matches single-thread's runloom_sched_yield fast path. */
    if (__builtin_expect(runloom_sched_ready_empty(&h->sched)
                         && runloom_cldeque_size(&h->deque) == 0
                         && h->sched.sleep_size == 0
                         && runloom_netpoll_parked_count() == 0
                         && runloom_blockpool_inflight() == 0, 1)) {
        /* The fast path deliberately does NOT look at h->sub_head -- work
         * delivered by mn_fiber (main thread) or wake_g (another hub) lands there,
         * and checking it lock-free on this hot path is fragile.  But that
         * means a g looping on sched_yield with momentarily-empty local queues
         * would spin forever and NEVER return to hub_main to drain sub_head, so
         * a fiber later mn_fiber'd / woken onto this hub starves (the multi-hub
         * sched_yield-fairness bug).  Bound it: after RUNLOOM_YIELD_FASTPATH_BOUND
         * consecutive trivial fast-paths, fall through to a real yield so
         * hub_main drains sub_head (under sub_lock, as always) + re-polls. */
        if (++runloom_tls_fastpath_streak < RUNLOOM_YIELD_FASTPATH_BOUND) {
            return 1;
        }
    }
    runloom_tls_fastpath_streak = 0;
    runloom_sched_ready_push(&h->sched, g);
    runloom_pystate_snap(&g->snap);
    runloom_tls_self_queued = 1;
    runloom_coro_yield();
    /* On return: hub_main has loaded g->snap, so we're back in our
     * own tstate slice and can keep running user code. */
    return 1;
}

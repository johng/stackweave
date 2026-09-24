/* Part of io_uring.c -- #included into that translation unit, not compiled
 * standalone.  See io_uring.c for the includes/typedefs/file-scope state
 * these definitions depend on. */

/* ============================================================
 * In-fiber delivery of signal-handler exceptions to fibers parked on an
 * io_uring op (both the single-thread scheduler and M:N hub paths).
 *
 * The scheduler hands a raised signal-handler exception (Ctrl-C / alarm) to a
 * blocked fiber IN-FIBER -- so the fiber's own try/except/finally runs -- by
 * waking a parked fiber with the exception stashed on its scheduler
 * (runloom_sched_drain -> runloom_netpoll_signal_wake, for NETPOLL parkers).  A
 * fiber blocked on an io_uring CQE is NOT a netpoll parker, so without a matching
 * hook the exception bypasses the fiber and is carried out of run(), skipping its
 * except/finally (backend-dependent control flow, audited bug).
 *
 * An io_uring parker registers a stack-allocated node in the process-wide list
 * below, then parks.  signal_wake marks one parker SIGNALED and wakes it; on
 * resume the parker restores the stashed exception into its own tstate and
 * returns -1, mirroring runloom_netpoll_wait_fd.
 *
 * A parker records the HUB it parked on (NULL for the single-thread scheduler)
 * because the two paths park differently and so must be woken differently:
 *
 *   hub == NULL  runloom_sched_park_safe  -> runloom_sched_wake_safe
 *   hub != NULL  runloom_sched_park_current + runloom_coro_yield
 *                                         -> runloom_mn_wake_g(hub, g)
 *
 * wake_safe is NOT a substitute for the M:N case: it only acts if it wins the
 * parked_safe 1->0 CAS, and park_current never sets parked_safe (nor park_hub,
 * which is what wake_safe would route on).  It would bump a wake_pending credit
 * nobody consumes and return having done nothing, leaving the fiber asleep --
 * which is exactly how this gap presented: the plumbing all existed, the walk
 * found the right waiter, and the wake was silently a no-op.
 *
 * Waking an M:N parker from the main thread while its CQE lands concurrently is
 * safe: runloom_mn_wake_g dedups both ways -- PARKED->QUEUED once under the
 * per-g wake_state machine (per-g-tstate mode), and the in_sub_queue 0->1 CAS in
 * runloom_mn_hub_submit otherwise -- so the loser of the race drops its wake
 * rather than enqueuing the g twice. */
typedef struct runloom_iou_sigwaiter {
    runloom_g_t                    *g;
    void                           *hub;     /* M:N hub parked on, NULL if single-thread */
    int                             signaled;
    struct runloom_iou_sigwaiter   *next;
    struct runloom_iou_sigwaiter  **pprev;   /* &(the pointer that points at us) */
} runloom_iou_sigwaiter_t;

/* ONE PROCESS-WIDE LIST, NOT THREAD-LOCAL.
 *
 * This was RUNLOOM_TLS, which made a hub thread's waiters invisible to the main
 * thread -- and the main thread is the only one that may run Python signal
 * handlers.  So under M:N a fiber blocked on an io_uring completion could never
 * be handed a Ctrl-C: it was not that the walk missed it, the list holding it
 * could not be reached from the thread doing the walking.
 *
 * Waking one cross-thread is already safe and always was: these waiters park
 * through runloom_sched_park_safe and are woken with runloom_sched_wake_safe,
 * the protocol built for exactly that (its wake_pending credit is what closes
 * the wake-before-park race).  Only the LOOKUP was thread-confined, so only the
 * lookup changes here -- no scheduling-state change, no new wake path.
 *
 * The lock covers link/unlink/walk.  These are I/O submission boundaries, not a
 * hot path: one acquire per parked io_uring op, and the walk runs only when a
 * signal handler has actually raised. */
static runloom_iou_sigwaiter_t *runloom_iou_sigwaiters;
static runloom_mutex_t runloom_iou_sigwaiter_lock;
static volatile int runloom_iou_sigwaiter_ready;   /* 0 = none, 2 = initialising, 1 = ready */

/* Lazy one-shot init, the same CAS-then-spin shape rl_cancel_lock uses in
 * io_uring_l_sys.c.inc -- a plain `if (!inited)` would let two hub threads both
 * mutex_init the same lock. */
static void runloom_iou_sigwaiter_lock_init(void)
{
    int expected = 0;
    if (__atomic_load_n(&runloom_iou_sigwaiter_ready, __ATOMIC_ACQUIRE) == 1) return;
    if (__atomic_compare_exchange_n(&runloom_iou_sigwaiter_ready, &expected, 2, 0,
                                    __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        runloom_mutex_init(&runloom_iou_sigwaiter_lock);
        __atomic_store_n(&runloom_iou_sigwaiter_ready, 1, __ATOMIC_RELEASE);
        return;
    }
    while (__atomic_load_n(&runloom_iou_sigwaiter_ready, __ATOMIC_ACQUIRE) != 1) { }
}

static void runloom_iou_sigwaiter_link(runloom_iou_sigwaiter_t *w,
                                       runloom_g_t *g, void *hub)
{
    runloom_iou_sigwaiter_lock_init();
    RUNLOOM_RLOCK(&runloom_iou_sigwaiter_lock, RUNLOOM_RANK_IOU_SIGWAITER);
    w->g        = g;
    w->hub      = hub;
    w->signaled = 0;
    w->next     = runloom_iou_sigwaiters;
    w->pprev    = &runloom_iou_sigwaiters;
    if (w->next != NULL) w->next->pprev = &w->next;
    runloom_iou_sigwaiters = w;
    RUNLOOM_RUNLOCK(&runloom_iou_sigwaiter_lock, RUNLOOM_RANK_IOU_SIGWAITER);
}

static void runloom_iou_sigwaiter_unlink(runloom_iou_sigwaiter_t *w)
{
    runloom_iou_sigwaiter_lock_init();
    RUNLOOM_RLOCK(&runloom_iou_sigwaiter_lock, RUNLOOM_RANK_IOU_SIGWAITER);
    if (w->pprev != NULL) {
        *w->pprev = w->next;
        if (w->next != NULL) w->next->pprev = w->pprev;
        w->pprev = NULL;
        w->next  = NULL;
    }
    RUNLOOM_RUNLOCK(&runloom_iou_sigwaiter_lock, RUNLOOM_RANK_IOU_SIGWAITER);
}

/* Restore the scheduler's stashed signal-handler exception into the CURRENT
 * (this fiber's) tstate, so it propagates out of the parked op through the
 * fiber's own stack.  Returns 1 if an exception was restored.  Mirrors the
 * RUNLOOM_NETPOLL_SIGNALED handling in runloom_netpoll_wait_fd. */
static int runloom_iou_deliver_signal(runloom_g_t *g)
{
    runloom_sched_t *owner = (g != NULL && g->owner != NULL)
                             ? g->owner : runloom_sched_get();
    /* ACQUIRE: signal_wake may have stashed this from the MAIN thread while this
     * fiber runs on a hub.  Pairs with its RELEASE store. */
    PyObject *exc = (owner != NULL)
                    ? __atomic_load_n(&owner->signal_exc, __ATOMIC_ACQUIRE) : NULL;
    if (owner != NULL) __atomic_store_n(&owner->signal_exc, NULL, __ATOMIC_RELAXED);
    if (exc != NULL) {
        PyObject *tb = PyException_GetTraceback(exc);   /* new ref or NULL */
        /* PyErr_Restore steals all three refs. */
        PyErr_Restore((PyObject *)Py_NewRef(Py_TYPE(exc)), exc, tb);
        return 1;
    }
    return 0;
}

/* Submit a global-ring ASYNC_CANCEL matching `target` by user_data so the kernel
 * completes `target` with -ECANCELED (best-effort; mirrors
 * runloom_iouring_cancel_g).  Used to unblock a parked single-shot op WITHOUT
 * returning while its SQE is still in flight -- which would leave the kernel
 * writing through the returned frame's op record (a stack use-after-free). */
static void runloom_iou_submit_cancel_global(runloom_iouring_op_t *target)
{
    runloom_iouring_op_t *cancel_op;
    struct io_uring_sqe sqe;
    cancel_op = rl_cancel_op_alloc();   /* pool-first: OOM can't skip the cancel */
    if (cancel_op == NULL) return;
    cancel_op->type = RUNLOOM_IOURING_OP_CANCEL;
    memset(&sqe, 0, sizeof(sqe));
    sqe.opcode = IORING_OP_ASYNC_CANCEL;
    sqe.addr   = (uint64_t)(uintptr_t)target;   /* match original SQE by user_data */
    sqe.fd     = -1;
    if (runloom_iouring_submit_sqe(sqe, cancel_op) != 0) rl_cancel_op_free(cancel_op);
}

/* Cancel a fiber parked on a single io_uring op (M:N hub path): submit an
 * IORING_OP_ASYNC_CANCEL targeting its user_data, so the kernel completes the op
 * with -ECANCELED and the drain wakes the fiber -- instead of the fiber hanging
 * until the op completes on its own.  Best-effort + idempotent: returns 1 if a
 * cancel was submitted, 0 if the fiber is not parked on a still-PARKED op.  A
 * stale target (the op was reaped between our read and the kernel match) just
 * yields -ENOENT; and since this fires only while the fiber is being torn down,
 * even cancelling its very next op serves the same intent.  The op record lives
 * on the parked fiber's live C stack, so reading op->wait here is safe while it
 * is genuinely PARKED.  The ring is multi-issuer under sub_lock, so any thread
 * may submit the cancel inline. */
int runloom_iouring_cancel_g(runloom_g_t *g)
{
    runloom_iouring_op_t *op;
    runloom_iouring_op_t *cancel_op;
    struct io_uring_sqe sqe;
    if (g == NULL) return 0;
    op = (runloom_iouring_op_t *)__atomic_load_n(&g->iouring_op, __ATOMIC_ACQUIRE);
    if (op == NULL || op->g != g) return 0;
    if (__atomic_load_n(&op->wait, __ATOMIC_ACQUIRE) != RUNLOOM_IOURING_WAIT_PARKED)
        return 0;
    cancel_op = rl_cancel_op_alloc();   /* pool-first: OOM can't skip the cancel */
    if (cancel_op == NULL) return 0;
    cancel_op->type = RUNLOOM_IOURING_OP_CANCEL;
    memset(&sqe, 0, sizeof(sqe));
    sqe.opcode = IORING_OP_ASYNC_CANCEL;
    sqe.addr   = (uint64_t)(uintptr_t)op;   /* match the original SQE by user_data */
    sqe.fd     = -1;
    if (runloom_iouring_submit_sqe(sqe, cancel_op) != 0) {
        rl_cancel_op_free(cancel_op);
        return 0;
    }
    return 1;
}

/* Hand the calling thread's stashed signal-handler exception to ONE io_uring
 * parker owned by that scheduler: mark it SIGNALED and wake it, so it restores
 * the exception in-fiber on resume.  Returns 1 if a parker took it, 0 if none are
 * registered (the scheduler then carries the exception out of run(), as before).
 * The io_uring analogue of runloom_netpoll_signal_wake; the single-thread drain
 * calls it after netpoll_signal_wake finds no parker. */
/* Exactly the test signal_wake's loop makes, without claiming anything: the
 * drain must know whether a taker exists BEFORE it consumes the signal. */
int runloom_iouring_has_sigwaiter(void)
{
    runloom_iou_sigwaiter_t *w;
    runloom_iou_sigwaiter_lock_init();
    RUNLOOM_RLOCK(&runloom_iou_sigwaiter_lock, RUNLOOM_RANK_IOU_SIGWAITER);
    for (w = runloom_iou_sigwaiters; w != NULL; w = w->next)
        if (w->g != NULL && !w->signaled) {
            RUNLOOM_RUNLOCK(&runloom_iou_sigwaiter_lock, RUNLOOM_RANK_IOU_SIGWAITER);
            return 1;
        }
    RUNLOOM_RUNLOCK(&runloom_iou_sigwaiter_lock, RUNLOOM_RANK_IOU_SIGWAITER);
    return 0;
}

int runloom_iouring_signal_wake(PyObject *exc)
{
    runloom_iou_sigwaiter_t *w;
    runloom_iou_sigwaiter_lock_init();
    RUNLOOM_RLOCK(&runloom_iou_sigwaiter_lock, RUNLOOM_RANK_IOU_SIGWAITER);
    for (w = runloom_iou_sigwaiters; w != NULL; w = w->next) {
        runloom_sched_t *tgt;
        PyObject *slot = NULL;
        if (w->g == NULL || w->signaled) continue;
        tgt = w->g->owner;
        if (tgt == NULL) continue;
        /* Reserve the TARGET's slot before marking the waiter -- the same order
         * runloom_netpoll_signal_wake uses, and for the same reason: marking
         * first and then finding the slot busy would leave a waiter flagged
         * `signaled` that nobody delivers to, and it would cancel its op and
         * surface a bare EINTR with no exception.  Under M:N the target is a
         * HUB's sched, so this store is read by another thread -- RELEASE pairs
         * with the ACQUIRE in runloom_iou_deliver_signal. */
        if (!__atomic_compare_exchange_n(&tgt->signal_exc, &slot, exc, 0,
                                         __ATOMIC_RELEASE, __ATOMIC_RELAXED))
            continue;                      /* target already holds one */
        w->signaled = 1;
        {
            runloom_g_t *wg  = w->g;
            void        *whub = w->hub;
            RUNLOOM_RUNLOCK(&runloom_iou_sigwaiter_lock, RUNLOOM_RANK_IOU_SIGWAITER);
            /* Outside the lock (both wakes can re-enter scheduler code), and
             * routed by how this waiter parked -- see the header comment: a hub
             * parker used park_current, which wake_safe cannot observe. */
            if (whub != NULL) runloom_mn_wake_g(whub, wg);
            else              runloom_sched_wake_safe(wg);
        }
        return 1;
    }
    RUNLOOM_RUNLOCK(&runloom_iou_sigwaiter_lock, RUNLOOM_RANK_IOU_SIGWAITER);
    return 0;
}


static runloom_iouring_ssize_t runloom_iouring_do(struct io_uring_sqe sqe)
{
    runloom_iouring_op_t op;
    void *hub;

    if (!runloom_iouring_available()) {
        errno = ENOSYS;
        return -1;
    }

    hub = runloom_mn_current_hub_opaque();
    op.type   = RUNLOOM_IOURING_OP_SINGLE;
    op.hub    = hub;
    op.g      = (hub != NULL) ? runloom_mn_tls_current_g()
                              : runloom_sched_get()->current;
    op.result = INT32_MIN;        /* sentinel: not yet completed */
    op.wait   = RUNLOOM_IOURING_WAIT_INFLIGHT;

    if (runloom_iouring_submit_sqe(sqe, &op) != 0) return -1;

    /* Inline drain after submit.  With IORING_FEAT_FAST_POLL (5.7+)
     * the kernel completes inline when data is already ready, so the
     * CQE is in the ring before io_uring_enter even returns.
     * Draining here turns "submit + park + pump + drain + wake +
     * resume" into "submit + drain + return" for the data-ready
     * case, saving an entire scheduler round-trip per RT in the
     * common echo workload.  Runs while op.wait == INFLIGHT, so for
     * OUR op the drain just sets result + flips wait to DONE (no wake);
     * we observe that below and skip the park. */
    runloom_iouring_drain();

    if (hub != NULL) {
        /* Hub (M:N) path: PARK -- do NOT block the OS thread.  A blocking
         * io_uring_enter here holds this hub's tstate ATTACHED across the
         * wait, which deadlocks any stop-the-world (free-threaded GC, the
         * per-g-tstate attach protocol) whose completion depends on a thread
         * frozen at the STW barrier -- and it monopolizes the hub so its
         * other fibers can't run.  Yield instead: the hub returns to its
         * (STW-safe) main loop, and when idle pumps the
         * shared ring's eventfd; a drainer wakes us via the wait handshake.
         * The inline drain above already completed us inline in the common
         * data-ready case (wait == DONE) -- skip the park then. */
        if (__atomic_load_n(&op.wait, __ATOMIC_ACQUIRE)
                != RUNLOOM_IOURING_WAIT_DONE) {
            int prev = RUNLOOM_IOURING_WAIT_INFLIGHT;
            if (__atomic_compare_exchange_n(&op.wait, &prev,
                                            RUNLOOM_IOURING_WAIT_PARKED, 0,
                                            __ATOMIC_ACQ_REL,
                                            __ATOMIC_ACQUIRE)) {
                RUNLOOM_IOU_NOTE(RUNLOOM_IOURING_WAIT_INFLIGHT,
                                 RUNLOOM_IOURING_WAIT_PARKED);
                /* Committed to park: any drainer completing the op now
                 * observes PARKED and wakes us.  park_current marks the g
                 * off-queue (hub_main won't re-enqueue on yield-return);
                 * the wake routes it back onto the hub sub-list, and the
                 * in_sub_queue CAS in hub_submit dedups a wake that races
                 * our coro_yield. */
                /* Publish the op so a cross-thread task.cancel can target it
                 * with an ASYNC_CANCEL (runloom_iouring_cancel_g).  Set after the
                 * PARKED CAS so the canceller's wait==PARKED gate is consistent;
                 * cleared on resume before the op leaves scope. */
                if (op.g != NULL)
                    __atomic_store_n(&op.g->iouring_op, &op, __ATOMIC_RELEASE);
                /* Cancel-and-RE-PARK loop, the same shape as the single-thread
                 * branch below and for the same reason: a signal must never
                 * return this frame while the single-shot SQE is still in
                 * flight, because the kernel writes the CQE through `op`, which
                 * lives on this stack -- a use-after-free -- and `op.result`
                 * would still be the INT32_MIN sentinel.  So on a signal we
                 * cancel the op and keep parking; the loop exits only once
                 * op.result is genuinely set (by the real completion or by the
                 * cancel's -ECANCELED), at which point nothing is in flight.
                 *
                 * RE-PARKING IS SOUND HERE.  `op.wait` is a one-way latch that
                 * only a completing drainer moves (PARKED -> DONE, via the
                 * exchange in runloom_iouring_drain), and it stores op.result
                 * BEFORE that exchange.  A signal wake does
                 * not touch op.wait, so on re-park it is still PARKED and the
                 * drainer's `prev == PARKED` test still fires our wake.  We do
                 * NOT redo the INFLIGHT->PARKED CAS -- there is nothing to redo.
                 *
                 * A drainer that completes while we are awake handling the
                 * signal wakes a RUNNING g; that is the ordinary spurious-wake
                 * case every park site in the scheduler already loops against,
                 * and mn_wake_g dedups it (wake_state PARKED->QUEUED once, or
                 * the in_sub_queue CAS in hub_submit). */
                {
                    runloom_iou_sigwaiter_t mn_sw;
                    int cancelled = 0;
                    runloom_iou_sigwaiter_link(&mn_sw, op.g, hub);
                    do {
                        runloom_sched_park_current();
                        runloom_coro_yield();
                        if (mn_sw.signaled && !cancelled) {
                            cancelled = 1;
                            runloom_iou_submit_cancel_global(&op);
                        }
                    } while (__atomic_load_n(&op.result, __ATOMIC_ACQUIRE)
                             == INT32_MIN);
                    runloom_iou_sigwaiter_unlink(&mn_sw);
                    if (op.g != NULL)
                        __atomic_store_n(&op.g->iouring_op, NULL,
                                         __ATOMIC_RELEASE);
                    if (mn_sw.signaled &&
                        __atomic_load_n(&op.result, __ATOMIC_ACQUIRE) < 0) {
                        /* Op ended cancelled/errored: deliver the signal
                         * in-fiber.  (If it raced to success we fall through
                         * and return the data; the hub drops the undelivered
                         * exception at teardown.) */
                        (void)runloom_iou_deliver_signal(op.g);
                        errno = EINTR;
                        return -1;
                    }
                }
            }
            /* else prev == DONE: a concurrent drainer completed the op
             * between the load and the CAS; op.result is set, fall through. */
        }
    } else if (op.g != NULL) {
        /* Single-thread sched path: park cooperatively.  When the
         * eventfd fires the netpoll pump calls runloom_iouring_drain
         * which calls runloom_sched_wake_safe(op.g).  park_safe yields
         * via runloom_coro_yield; on resume we eat the pending wake.
         *
         * RE-PARK on a SPURIOUS wake: park_safe returns on ANY wake, but the
         * generic park can be resumed by a stale/duplicate wake before the CQE
         * drain has set op.result.  Returning then would hand back the INT32_MIN
         * sentinel as the result AND leave the real completion's later wake to
         * land on an unparked g (dropped).  Loop until op.result is actually set
         * -- the same "park until the condition holds" discipline as the chan /
         * netpoll park loops.  park_safe's wake_pending Dekker makes
         * the recheck-then-re-park race-free.  (audit round-3 single-op lost wake.) */
        {
            runloom_iou_sigwaiter_t sw;
            int cancelled = 0;
            /* Register for in-fiber signal delivery (see runloom_iou_sigwaiter_*):
             * a Ctrl-C/alarm handler that raises while we're parked should
             * propagate through THIS fiber, not out of run(). */
            runloom_iou_sigwaiter_link(&sw, op.g, NULL);
            do {
                runloom_sched_park_safe();
                if (sw.signaled && !cancelled) {
                    /* Signal raised for this fiber.  We must NOT return while the
                     * single-shot SQE is still in flight -- its CQE would later
                     * write through this frame's `op` (a stack use-after-free) --
                     * so cancel the op and keep parking until its terminal
                     * (-ECANCELED) CQE lands; surface the signal afterwards. */
                    cancelled = 1;
                    runloom_iou_submit_cancel_global(&op);
                }
            } while (__atomic_load_n(&op.result, __ATOMIC_ACQUIRE) == INT32_MIN);
            runloom_iou_sigwaiter_unlink(&sw);
            if (sw.signaled &&
                __atomic_load_n(&op.result, __ATOMIC_ACQUIRE) < 0) {
                /* Op ended cancelled/errored: deliver the signal in-fiber.  (If it
                 * raced to success we fall through and return the data; the
                 * scheduler drops the undelivered exception at drain exit.) */
                (void)runloom_iou_deliver_signal(op.g);
                errno = EINTR;
                return -1;
            }
        }
    } else {
        /* Not inside a fiber (plain thread / main thread calling
         * file_read directly).  Block, but RELEASE the tstate around the
         * wait so a stop-the-world isn't held off by this syscall. */
        while (__atomic_load_n(&op.result, __ATOMIC_ACQUIRE) == INT32_MIN) {
            int n;
            Py_BEGIN_ALLOW_THREADS
            n = sys_io_uring_enter(runloom_iouring_state.ring_fd, 0, 1,
                                   IORING_ENTER_GETEVENTS, NULL, 0);
            Py_END_ALLOW_THREADS
            if (n < 0 && errno != EINTR) return -1;
            runloom_iouring_drain();
        }
    }

    {
        int32_t r = __atomic_load_n(&op.result, __ATOMIC_ACQUIRE);
#if defined(RUNLOOM_FSM_VALIDATE)
        /* Route the io_uring CQE completion through the SAME total I/O FSM as the
         * sync backends (its res<0 is -errno).  One NOTE here covers every
         * io_uring op; READ/RECV classify like recv, WRITE/SEND like send
         * (pread/pwrite of regular files share that >=0 / <0-errno shape). */
        {
            int io_err = (r < 0) ? -(int)r : 0;
            switch (sqe.opcode) {
            case IORING_OP_READ:  case IORING_OP_RECV:
                RUNLOOM_IO_NOTE(RUNLOOM_IO_RECV,
                    runloom_io_classify(RUNLOOM_IO_RECV, (long long)r, io_err));
                break;
            case IORING_OP_WRITE: case IORING_OP_SEND:
                RUNLOOM_IO_NOTE(RUNLOOM_IO_SEND,
                    runloom_io_classify(RUNLOOM_IO_SEND, (long long)r, io_err));
                break;
            default: break;   /* other opcodes are not byte-stream I/O */
            }
        }
#endif
        if (r < 0) { errno = -r; return -1; }
        return r;
    }
}

runloom_iouring_ssize_t runloom_iouring_pread(int fd, void *buf, size_t n,
                                        runloom_iouring_off_t offset)
{
    struct io_uring_sqe sqe;
    memset(&sqe, 0, sizeof(sqe));
    sqe.opcode = IORING_OP_READ;
    sqe.fd     = fd;
    sqe.addr   = (uintptr_t)buf;
    sqe.len    = (unsigned)n;
    sqe.off    = (uint64_t)offset;
    return runloom_iouring_do(sqe);
}

runloom_iouring_ssize_t runloom_iouring_pwrite(int fd, const void *buf, size_t n,
                                         runloom_iouring_off_t offset)
{
    struct io_uring_sqe sqe;
    memset(&sqe, 0, sizeof(sqe));
    sqe.opcode = IORING_OP_WRITE;
    sqe.fd     = fd;
    sqe.addr   = (uintptr_t)buf;
    sqe.len    = (unsigned)n;
    sqe.off    = (uint64_t)offset;
    return runloom_iouring_do(sqe);
}

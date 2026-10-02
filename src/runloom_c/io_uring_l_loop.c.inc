/* Part of io_uring.c -- #included into that translation unit, not compiled
 * standalone.  See io_uring.c for the includes/typedefs/file-scope state
 * these definitions depend on.
 *
 * io_uring-as-loop backend (RUNLOOM_IOURING_LOOP=1, default OFF).
 * ----------------------------------------------------------------------------
 * The default M:N hub blocks in epoll_wait (runloom_netpoll_pump) and io_uring
 * completions are delivered indirectly: each per-hub ring registers an eventfd
 * into the shared epoll set, the pump wakes on the eventfd and drains the ring.
 * That eventfd -> epoll -> pump -> drain bridge thrashes under socket load.
 *
 * With the loop backend the hub blocks DIRECTLY in its own ring via
 * io_uring_submit_and_wait_timeout (the kernel's native EXT_ARG timeout, no
 * SQE/CQE, Linux 5.11+) -- the ring IS the event loop, per the liburing /
 * gain / tokio-uring blueprint.  Two non-io_uring wake sources are bridged in
 * as persistent multishot POLL_ADDs identified by sentinel user_data:
 *
 *   - the hub's per-hub wake eventfd (RUNLOOM_LOOP_UD_WAKE): a cross-hub submit
 *     (runloom_mn_hub_submit) writes it to interrupt a ring-blocked hub, so a
 *     newly-submitted fiber is run promptly instead of after the idle tick.
 *   - the shared netpoll epoll fd (RUNLOOM_LOOP_UD_EPOLL): every existing
 *     wait_fd-based readiness path is UNCHANGED (it still epoll_ctl-registers
 *     and links a parker); when the epoll fd becomes readable the loop reports
 *     it and the hub does a non-blocking runloom_netpoll_pump(0) to drain the
 *     ready fds -> wake their parkers.  This keeps the whole parker/commit/
 *     by-fd/deadline machinery untouched -- only the block primitive changes.
 *
 * Ordinary op records (recv/send/..., user_data = an 8-byte-aligned pointer,
 * never a tiny sentinel) are drained inline here exactly as
 * runloom_iouring_ring_drain does -- that is the Stage-2 proactor hot path.
 *
 * Reaping is per-hub (DEFER_TASKRUN: only the ring's creating thread may
 * io_uring_enter(GETEVENTS)), so the loop drain runs single-reaper and needs
 * no per-CQE CAS (unlike runloom_iouring_ring_drain, which can race the shared
 * epoll pump on another idle hub).
 *
 * Cross-hub migration.  Every park is a possible migration: the woken fiber
 * resumes on whichever hub pulls it.  Each ring has exactly one SQ producer and
 * one CQ reaper, its hub thread, with no locks, so a fiber may only write SQEs
 * into the ring of the hub it is running on right now.  Two rules keep that:
 *   - a single-shot op (loop_recv / loop_send) resolves the CURRENT hub's ring
 *     at submit and takes no ring argument, so no caller can hold a ring across
 *     a park.  The CQE is reaped by the submitting hub, which wakes the fiber
 *     wherever it will resume; the op record lives on the fiber's own stack.
 *   - a multishot recv handle is bound to one ring and its provided-buffer
 *     ring for its whole life, but its fiber is not: it reads delivered chunks
 *     from any hub, and off the owner hub it posts buffer returns, re-arms and
 *     its close to the owner's inbox, which the owner drains at the top of its
 *     loop (runloom_iouring_loop_inbox_drain).
 * Before these rules the all-C echo cached its hub's ring for the whole
 * connection; after a migration it wrote SQEs into another hub's ring from a
 * foreign thread, and a lost SQE left its fiber parked forever (a few percent
 * of echo runs at 2 and 4 hubs).
 *
 * Servicing.  The ring is serviced every scheduling round, never only at hub
 * idle: hub_main calls runloom_iouring_loop_poll (submit deferred SQEs, post
 * and drain completions, non-blocking) when its local queues run dry, BEFORE
 * the global run-queue and stealing -- Go's netpoll(0) step in findrunnable --
 * and every 64 turns while busy; loop_wait is only the blocking form of the
 * same step for a hub with nothing at all to run.  Under M:N a hub fed by the
 * global queue or by stealing rarely idles, so an idle-only service left this
 * ring's ops unsubmitted and its completions unread for whole busy stretches
 * (4-19x below epoll on the co-located p223 echo, worse with more hubs).
 * Single-shot ops are also DIRECT-FIRST (loop_recv / loop_send): a plain
 * non-blocking syscall, and the ring only when that would block, so the ring
 * is the wait primitive with the data copied on completion rather than the
 * carrier of every op.  Together these put the loop backend at epoll parity
 * (single-shot) to slightly above it (multishot) on p223.
 */

/* user_data sentinels for the persistent infrastructure polls.  Op records are
 * heap/stack pointers (page-ish addresses, 8-byte aligned), never these. */
#define RUNLOOM_LOOP_UD_WAKE   ((uint64_t)1)
#define RUNLOOM_LOOP_UD_EPOLL  ((uint64_t)2)

/* Bits OR'd into the caller's flags by the drain. */
#define RUNLOOM_LOOP_F_EPOLL   0x1   /* epoll fd readable: caller should pump(0) */
#define RUNLOOM_LOOP_F_WAKE    0x2   /* wake eventfd fired: re-drain the sub list */

/* Read RUNLOOM_IOURING_LOOP once.  1 = loop backend on.  Linux-only path.
 * Forced OFF under RUNLOOM_SIM (MN_SIM_DST_PLAN.md I3 gate [2]): with sim on,
 * hub rings would still be created and hubs would block in loop_wait with CQE
 * reapers waking gs -- a real-kernel readiness plane the seed cannot order.
 * The existing iouring_available sim gate (io_uring_l_buf.c.inc, 03de0ec1)
 * covers only the TCP fast path, NOT ring creation.  This gate lives in THIS
 * TU deliberately: the mn_sched TU lacks <linux/io_uring.h>, so an IORING_*
 * #if gate there silently compiles to a no-op stub (the UAPI-macro TU trap). */
int runloom_iouring_loop_enabled(void)
{
    static int v = -1;
    int cur = __atomic_load_n(&v, __ATOMIC_RELAXED);
    if (cur < 0) {
        const char *e = getenv("STACKWEAVE_IOURING_LOOP");
        cur = (e != NULL && *e != '0' && *e != '\0') ? 1 : 0;
        if (runloom_sim_enabled()) cur = 0;
        __atomic_store_n(&v, cur, __ATOMIC_RELAXED);
    }
    return cur;
}

/* Burn-in belt-and-suspenders: RUNLOOM_IOURING_LOOP_PUMP_ALWAYS=1 restores the
 * old every-idle-tick pump(0) symptom-guard on top of the one-shot fix.  Default
 * OFF -- the one-shot epoll poll now re-derives the epoll fd's level every wait
 * cycle, so F_EPOLL is reliable and the unconditional pump is redundant (kept
 * available in case a soak surfaces a residual path). */
int runloom_iouring_loop_pump_always(void)
{
    static int v = -1;
    int cur = __atomic_load_n(&v, __ATOMIC_RELAXED);
    if (cur < 0) {
        const char *e = getenv("STACKWEAVE_IOURING_LOOP_PUMP_ALWAYS");
        cur = (e != NULL && *e != '0' && *e != '\0') ? 1 : 0;
        __atomic_store_n(&v, cur, __ATOMIC_RELAXED);
    }
    return cur;
}

/* ======================= Stage-3 multishot recv ===========================
 * One persistent IORING_OP_RECV | MULTISHOT | BUFFER_SELECT SQE per connection
 * delivers a CQE per chunk into a PER-HUB provided buffer ring -- no re-submit
 * and no submit/park per recv, amortising the per-op cost the single-shot
 * Stage-2 path pays.  The ring's owning hub thread is the sole SQE issuer
 * (SINGLE_ISSUER), the sole CQE reaper (loop_drain) and the sole writer of the
 * buffer ring, so none of that is locked.  The consumer fiber migrates freely:
 * it reads delivered chunks from any hub and hands buffer returns, re-arms and
 * its close to the owner through the ring's inbox (see "OWNER VS CONSUMER" at
 * the handle below).  Gated behind RUNLOOM_IOURING_MS (requires the loop
 * backend), default off. */

#ifndef RUNLOOM_LOOP_MS_BGID
#  define RUNLOOM_LOOP_MS_BGID 0
#endif
#define RUNLOOM_LOOP_MS_PBUF_COUNT_DEFAULT 1024
#define RUNLOOM_LOOP_MS_PBUF_SIZE          2048
/* user_data sentinel for the ASYNC_CANCEL a multishot close submits; its own
 * completion CQE is discarded by loop_drain (op records are 8-byte-aligned
 * pointers, never this tiny value, alongside UD_WAKE=1 / UD_EPOLL=2). */
#define RUNLOOM_LOOP_UD_MSCANCEL  ((uint64_t)3)

typedef struct runloom_loop_ms runloom_loop_ms_t;
static void runloom_loop_ms_on_cqe(runloom_loop_ms_t *h,
                                   int32_t res, uint32_t flags);

/* 1 if the multishot recv path is enabled (RUNLOOM_IOURING_MS set); read once.
 * Only consulted on the loop backend. */
int runloom_iouring_loop_ms_enabled(void)
{
    static int v = -1;
    int cur = __atomic_load_n(&v, __ATOMIC_RELAXED);
    if (cur < 0) {
        const char *e = getenv("STACKWEAVE_IOURING_MS");
        cur = (e != NULL && *e != '0' && *e != '\0') ? 1 : 0;
        __atomic_store_n(&v, cur, __ATOMIC_RELAXED);
    }
    return cur;
}

/* True iff SOME io_uring feature is opted in -- the loop backend
 * (RUNLOOM_IOURING_LOOP), multishot recv (RUNLOOM_IOURING_MS, which REQUIRES
 * loop, so loop_enabled already covers it), or the TCP proactor transport
 * (RUNLOOM_TCPCONN_IOURING=1|auto).  All default OFF.  When none is set, the
 * per-hub io_uring ring carries no ops and its periodic GETEVENTS flush is pure
 * waste, so mn_sched_hub_main skips creating the ring.  Read once. */
int runloom_iouring_any_enabled(void)
{
    static int v = -1;
    int cur = __atomic_load_n(&v, __ATOMIC_RELAXED);
    if (cur < 0) {
        const char *t = getenv("STACKWEAVE_TCPCONN_IOURING");
        int tcp = (t != NULL && (t[0] == '1' || strcmp(t, "auto") == 0)) ? 1 : 0;
        cur = (runloom_iouring_loop_enabled() || tcp) ? 1 : 0;
        if (runloom_sim_enabled()) cur = 0;   /* I3 gate [2]: no hub rings under sim */
        __atomic_store_n(&v, cur, __ATOMIC_RELAXED);
    }
    return cur;
}

/* Per-hub provided buffer ring count (env RUNLOOM_IOURING_MS_BUFS, default
 * 1024).  Rounded up to a power of two, clamped to [16, 65536] (bid is 16-bit). */
static unsigned runloom_loop_ms_pbuf_count(void)
{
    static int v = -1;
    int cur = __atomic_load_n(&v, __ATOMIC_RELAXED);
    if (cur < 0) {
        const char *e = getenv("STACKWEAVE_IOURING_MS_BUFS");
        long n = (e != NULL) ? atol(e) : RUNLOOM_LOOP_MS_PBUF_COUNT_DEFAULT;
        unsigned p = 16;
        if (n < 16) n = 16;
        if (n > 65536) n = 65536;
        while (p < (unsigned)n) p <<= 1;
        cur = (int)p;
        __atomic_store_n(&v, cur, __ATOMIC_RELAXED);
    }
    return (unsigned)cur;
}

/* Multishot outcomes, as process totals updated where they happen (once per
 * hub at arm, once per stream at open: rare next to the per-chunk path).  A
 * STACKWEAVE_IOURING_MS run whose streams all fell back to single-shot moves
 * the same bytes as one that ran multishot, so without these the two look
 * identical from outside: stats()["iouring_loop_ms_opens"] /
 * ["iouring_loop_ms_fallbacks"] / ["iouring_loop_ms_pbuf_errno"]. */
static unsigned long long runloom_iouring_loop_ms_opens_total = 0;
static unsigned long long runloom_iouring_loop_ms_fallbacks_total = 0;
static int runloom_iouring_loop_ms_pbuf_errno_last = 0;

unsigned long long runloom_iouring_loop_ms_opens(void)
{
    return __atomic_load_n(&runloom_iouring_loop_ms_opens_total, __ATOMIC_RELAXED);
}

unsigned long long runloom_iouring_loop_ms_fallbacks(void)
{
    return __atomic_load_n(&runloom_iouring_loop_ms_fallbacks_total,
                           __ATOMIC_RELAXED);
}

int runloom_iouring_loop_ms_pbuf_errno(void)
{
    return __atomic_load_n(&runloom_iouring_loop_ms_pbuf_errno_last,
                           __ATOMIC_RELAXED);
}

/* A hub's buffer ring could not be set up, so every stream opened on that hub
 * falls back to single-shot recv.  Record why, and say so once: the env var
 * asked for multishot, and the fallback is otherwise silent.  Ubuntu's 6.8
 * kernels, which refuse every plain registration, no longer land here: the
 * workaround in io_uring_l_pbuf.c.inc registers on them (unless
 * STACKWEAVE_IOURING_PBUF_RESV_QUIRK=0). */
static void runloom_iouring_loop_ms_pbuf_failed(int err)
{
    static int warned = 0;
    __atomic_store_n(&runloom_iouring_loop_ms_pbuf_errno_last, err,
                     __ATOMIC_RELAXED);
    if (__atomic_exchange_n(&warned, 1, __ATOMIC_RELAXED) == 0)
        fprintf(stderr, "[stackweave] io_uring capability degrade: the hub "
                "provided buffer ring could not be registered (errno %d, %s) "
                "-- STACKWEAVE_IOURING_MS streams fall back to single-shot "
                "recv\n", err, strerror(err));
}

/* Register a provided buffer ring on THIS hub ring (bgid RUNLOOM_LOOP_MS_BGID).
 * Returns 0 on success, -1 on failure (caller leaves ms_pool NULL -> multishot
 * ms_open returns NULL -> the echo falls back to single-shot).  Mirrors the
 * process-global pool setup in io_uring_l_sys.c.inc, but per-ring and lock-free
 * (the owning hub is the sole tail producer). */
static int runloom_iouring_loop_ms_pbuf_setup(runloom_iouring_ring_t *r)
{
    struct runloom_iouring_buf_reg reg;
    struct runloom_iouring_buf *bufs;
    unsigned count = runloom_loop_ms_pbuf_count();
    unsigned size  = RUNLOOM_LOOP_MS_PBUF_SIZE;
    size_t pool_total = (size_t)count * size;
    size_t bring_size = sizeof(struct runloom_iouring_buf) * count;
    long page = sysconf(_SC_PAGESIZE);
    void *pool = NULL, *bring = NULL;
    unsigned i;
    if (page <= 0) page = 4096;
    if (posix_memalign(&pool, (size_t)page, pool_total) != 0)  pool = NULL;
    if (posix_memalign(&bring, (size_t)page, bring_size) != 0) bring = NULL;
    if (pool == NULL || bring == NULL) {
        free(pool); free(bring);
        runloom_iouring_loop_ms_pbuf_failed(ENOMEM);
        return -1;
    }
    memset(bring, 0, bring_size);
    memset(&reg, 0, sizeof(reg));
    reg.ring_addr    = (uintptr_t)bring;
    reg.ring_entries = count;
    reg.bgid         = RUNLOOM_LOOP_MS_BGID;
    reg.flags        = 0;
    if (runloom_iouring_pbuf_ring_register(r->ring_fd, &reg) != 0) {
        int err = errno;
        free(pool); free(bring);
        runloom_iouring_loop_ms_pbuf_failed(err);
        return -1;
    }
    bufs = (struct runloom_iouring_buf *)bring;
    for (i = 0; i < count; i++) {
        bufs[i].addr = (uintptr_t)((char *)pool + (size_t)i * size);
        bufs[i].len  = size;
        bufs[i].bid  = (uint16_t)i;
        bufs[i].resv = 0;
    }
    /* Kernel tail overlays bufs[0].resv; release so it sees all descriptors. */
    __atomic_store_n(&bufs[0].resv, (uint16_t)count, __ATOMIC_RELEASE);
    r->ms_pool  = pool;
    r->ms_bring = bring;
    r->ms_count = count;
    r->ms_size  = size;
    r->ms_mask  = count - 1;
    return 0;
}

static void runloom_iouring_loop_ms_pbuf_teardown(runloom_iouring_ring_t *r)
{
    if (r == NULL || r->ms_pool == NULL) return;
    /* Unregister BEFORE freeing so the kernel drops its reference to bring/pool
     * (the ring fd is closed right after, which would also drop it, but the
     * close happens in ring_destroy after this returns).  If the kernel keeps
     * the ring, it may still fill a buffer from it until that close, so leak
     * both (once per hub ring, at shutdown) rather than hand them back to
     * malloc while the kernel can write into them. */
    if (runloom_iouring_pbuf_ring_unregister(r->ring_fd,
                                             RUNLOOM_LOOP_MS_BGID) == 0) {
        free(r->ms_pool);
        free(r->ms_bring);
    }
    r->ms_pool = NULL;
    r->ms_bring = NULL;
    r->ms_count = r->ms_size = r->ms_mask = 0;
}

static void *runloom_iouring_loop_ms_buf_addr(runloom_iouring_ring_t *r,
                                              unsigned bid)
{
    if (r->ms_pool == NULL || bid >= r->ms_count) return NULL;
    return (char *)r->ms_pool + (size_t)bid * r->ms_size;
}

/* Return a consumed buffer to this hub's ring (lock-free: the owning hub is the
 * sole tail producer). */
static void runloom_iouring_loop_ms_buf_return(runloom_iouring_ring_t *r,
                                               unsigned bid)
{
    struct runloom_iouring_buf *bufs;
    uint16_t tail, idx;
    if (r->ms_pool == NULL || bid >= r->ms_count) return;
    bufs = (struct runloom_iouring_buf *)r->ms_bring;
    tail = __atomic_load_n(&bufs[0].resv, __ATOMIC_ACQUIRE);
    idx  = (uint16_t)(tail & r->ms_mask);
    bufs[idx].addr = (uintptr_t)((char *)r->ms_pool + (size_t)bid * r->ms_size);
    bufs[idx].len  = r->ms_size;
    bufs[idx].bid  = (uint16_t)bid;
    __atomic_store_n(&bufs[0].resv, (uint16_t)(tail + 1), __ATOMIC_RELEASE);
}

/* Write one POLL_ADD(fd, POLLIN) SQE into the SQ with `ud` as user_data.  Does
 * NOT enter the kernel (deferred); the next loop_wait submits it.  Does NOT
 * touch the inflight counter -- these polls are infrastructure, always armed,
 * and must not keep the hub "busy".  Single-issuer: caller is the ring's owning
 * hub.
 *
 * `multishot`: a MULTISHOT poll (IORING_POLL_ADD_MULTI) stays armed and re-fires
 * per readiness TRANSITION, but the kernel never re-samples the fd's standing
 * LEVEL between transitions.  That is fatal when polling the SHARED EPOLL FD: an
 * interior socket left LEVEL-readable with no fresh transition (a partial recv +
 * re-park, a >64-event pump truncation, a late-linked parker whose readiness was
 * only stashed) is invisible to a standing multishot -> no CQE -> the parked
 * reader hangs (the reproduced loop-backend hang).  A ONE-SHOT poll instead runs
 * an arm-time vfs_poll that re-samples current LEVEL and fires immediately if the
 * fd is still readable -- so re-arming it every cycle makes the bridge re-derive
 * ground truth exactly like plain epoll_wait.  Use multishot=0 for the epoll fd;
 * the wake eventfd can stay multishot (its miss is masked by the Dekker sub_head
 * re-check + the global-ring direct drain). */
static void runloom_iouring_loop_enqueue_poll(runloom_iouring_ring_t *r,
                                              int fd, uint64_t ud, int multishot)
{
    unsigned tail = __atomic_load_n(r->sq_tail, __ATOMIC_RELAXED);
    unsigned idx  = tail & r->sq_mask;
    struct io_uring_sqe *sqe = &r->sqes[idx];
    memset(sqe, 0, sizeof(*sqe));
    sqe->opcode        = IORING_OP_POLL_ADD;
    sqe->fd            = fd;
    sqe->poll32_events = POLLIN;
    sqe->len           = multishot ? IORING_POLL_ADD_MULTI : 0;
    sqe->user_data     = ud;
    r->sq_array[idx]   = idx;
    __atomic_store_n(r->sq_tail, tail + 1, __ATOMIC_RELEASE);
}

/* Submit every pending (un-consumed) SQE on the ring with a non-blocking
 * io_uring_enter, advancing sq_submitted_tail by what the kernel consumed.
 * EINTR-safe.  No-op when nothing is pending (the common Stage-1 case: the only
 * SQEs are the one-time poll arms, already submitted). */
static void runloom_iouring_loop_flush(runloom_iouring_ring_t *r)
{
    unsigned tail = __atomic_load_n(r->sq_tail, __ATOMIC_ACQUIRE);
    unsigned sub  = r->sq_submitted_tail;
    int n;
    if (tail == sub) return;
    n = sys_io_uring_submit_eintr(r->ring_fd, tail - sub);
    if (n > 0) r->sq_submitted_tail = sub + (unsigned)n;
}

/* Drain every CQE currently in the ring (single-reaper -- this hub only).
 * Sentinel CQEs set flags (and re-arm a dead multishot poll); op-record CQEs
 * publish their result and wake the parked fiber via the op->wait
 * handshake.  Advances the CQ head once for the whole batch.  Returns the
 * number of OP completions (sentinel polls excluded): what loop_poll's caller
 * uses to decide whether anything runnable may have landed. */
static int runloom_iouring_loop_drain(runloom_iouring_ring_t *r, int *flags_out)
{
    unsigned head, tail, i, seen = 0;
    int ops = 0;
    if (r == NULL) return 0;
    head = __atomic_load_n(r->cq_head, __ATOMIC_ACQUIRE);
    tail = __atomic_load_n(r->cq_tail, __ATOMIC_ACQUIRE);
    /* The single-reaper CQE dispatch runs on the hub BETWEEN goroutines: it
     * wakes parked fibers, delivers multishot chunks, and reclaims cancel
     * records.  A cooperative yield/park here would re-enter the scheduler
     * mid-reap (a hub-reentrancy bug), so declare it a no-yield region --
     * RUNLOOM_CTXCHECK then asserts it at coro_yield (item #1 might_switch).
     * Zero cost when CTXCHECK is off. */
    RUNLOOM_NOYIELD_BEGIN();
    for (i = head; i != tail; i++) {
        struct io_uring_cqe *cqe = &r->cqes[i & r->cq_mask];
        uint64_t ud     = cqe->user_data;
        int32_t  res    = cqe->res;
        uint32_t cflags = cqe->flags;
        seen++;
        if (ud == RUNLOOM_LOOP_UD_WAKE) {
            if (flags_out) *flags_out |= RUNLOOM_LOOP_F_WAKE;
            /* Multishot poll de-armed (should not happen for a long-lived
             * eventfd, but F_MORE clear means it won't fire again): re-arm. */
            if (!(cflags & IORING_CQE_F_MORE) && r->loop_wake_fd >= 0)
                runloom_iouring_loop_enqueue_poll(r, r->loop_wake_fd,
                                                  RUNLOOM_LOOP_UD_WAKE, /*multishot=*/1);
            continue;
        }
        if (ud == RUNLOOM_LOOP_UD_EPOLL) {
            if (flags_out) *flags_out |= RUNLOOM_LOOP_F_EPOLL;
            /* ONE-SHOT re-arm, UNCONDITIONALLY (a one-shot CQE is always
             * terminal; no F_MORE).  loop_flush submits this deferred SQE before
             * the next blocking enter, and a one-shot POLL_ADD's arm-time
             * vfs_poll re-samples the epoll fd's CURRENT level -- firing an
             * immediate CQE if it is still readable.  So every wait cycle begins
             * with a fresh ground-truth sample, closing the standing-multishot
             * edge-drop (see the enqueue_poll header). */
            if (r->loop_epoll_fd >= 0)
                runloom_iouring_loop_enqueue_poll(r, r->loop_epoll_fd,
                                                  RUNLOOM_LOOP_UD_EPOLL, /*multishot=*/0);
            continue;
        }
        if (ud == RUNLOOM_LOOP_UD_MSCANCEL) {
            /* ASYNC_CANCEL completion for a closed multishot: nothing to do --
             * the cancelled multishot's own terminal (no-F_MORE) CQE is what
             * frees its handle (loop_ms_on_cqe, closing).  Did not bump
             * inflight, so don't decrement. */
            continue;
        }
        /* Op record (Stage-2 proactor recv/send, or a Stage-3 multishot recv).
         * Single-reaper, so no CAS on the CQ -- but the op->wait handshake still
         * gates the SINGLE wake so a not-yet-parked submitter (impossible
         * same-thread, but kept for the shared-ring safety contract) isn't
         * double-resumed. */
        ops++;
        {
            runloom_iouring_op_t *op = (runloom_iouring_op_t *)(uintptr_t)ud;
            if (op != NULL && op->type == RUNLOOM_IOURING_OP_MULTISHOT) {
                /* Stage-3 multishot recv: deliver the buffer / EOF / error to
                 * the per-connection handle (op is its first field).  The op
                 * stays armed on the SAME SQE across chunks (F_MORE); only its
                 * terminal CQE (no F_MORE) ends it, so decrement inflight just
                 * then -- submit and each re-submit bumped it once. */
                runloom_loop_ms_on_cqe((runloom_loop_ms_t *)op, res, cflags);
                if (!(cflags & IORING_CQE_F_MORE)) {
                    __atomic_sub_fetch(&r->inflight, 1, __ATOMIC_ACQ_REL);
                    __atomic_sub_fetch(&runloom_iouring_inflight_count, 1,
                                       __ATOMIC_ACQ_REL);
                }
                continue;
            }
            if (op != NULL && op->type == RUNLOOM_IOURING_OP_CANCEL) {
                /* Fire-and-forget ASYNC_CANCEL record (TCPCONN close routes an
                 * OP_CANCEL-with-dup-fd broadcast to THIS hub ring under the loop
                 * backend; ring_drain now delegates here so this is the one place
                 * that reaps them).  Close any dup'd fd it owns, reclaim the
                 * record, decrement inflight.  WITHOUT this branch the generic
                 * path below silently leaks both the record and the dup fd -- an
                 * EMFILE creep across close cycles (the same leak ring_drain's
                 * OP_CANCEL branch guards against). */
                if (op->cancel_dup_fd >= 0) close(op->cancel_dup_fd);
                rl_cancel_op_free(op);
                __atomic_sub_fetch(&r->inflight, 1, __ATOMIC_ACQ_REL);
                __atomic_sub_fetch(&runloom_iouring_inflight_count, 1,
                                   __ATOMIC_ACQ_REL);
                continue;
            }
            if (op != NULL) {
                __atomic_store_n(&op->result, res, __ATOMIC_RELEASE);
                if (op->hub != NULL) {
                    int prev = __atomic_exchange_n(&op->wait,
                                                   RUNLOOM_IOURING_WAIT_DONE,
                                                   __ATOMIC_ACQ_REL);
                    RUNLOOM_IOU_NOTE(prev, RUNLOOM_IOURING_WAIT_DONE);
                    if (prev == RUNLOOM_IOURING_WAIT_PARKED)
                        runloom_mn_wake_g(op->hub, op->g);
                } else if (op->g != NULL) {
                    runloom_sched_wake_safe(op->g);
                }
            }
            __atomic_sub_fetch(&r->inflight, 1, __ATOMIC_ACQ_REL);
            __atomic_sub_fetch(&runloom_iouring_inflight_count, 1,
                               __ATOMIC_ACQ_REL);
        }
    }
    RUNLOOM_NOYIELD_END();
    if (seen) __atomic_store_n(r->cq_head, head + seen, __ATOMIC_RELEASE);
    return ops;
}

/* Create the hub's wake eventfd and arm the two persistent multishot polls
 * (wake eventfd + shared epoll fd) on its ring.  Called once from hub_main
 * after the ring is created, only when the loop backend is on.  Returns the
 * wake fd (>=0) on success; -1 on any failure (caller then discards the ring
 * and the hub falls back to the epoll pump, so no half-armed state survives).
 * epoll_fd may be -1 (no epoll backend) -- then only the wake poll is armed. */
int runloom_iouring_loop_hub_arm(runloom_iouring_ring_t *r, int epoll_fd)
{
    int wfd;
    if (r == NULL) return -1;
    wfd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    if (wfd < 0) return -1;
    r->loop_wake_fd  = wfd;
    r->loop_epoll_fd = epoll_fd;
    runloom_iouring_loop_enqueue_poll(r, wfd, RUNLOOM_LOOP_UD_WAKE, /*multishot=*/1);
    if (epoll_fd >= 0)   /* ONE-SHOT: re-derives the epoll fd's level each cycle */
        runloom_iouring_loop_enqueue_poll(r, epoll_fd, RUNLOOM_LOOP_UD_EPOLL, /*multishot=*/0);
    /* Submit the arms now (non-blocking) so they are live before the first
     * wait.  On failure, undo: the caller will discard the ring. */
    runloom_iouring_loop_flush(r);
    if (r->sq_submitted_tail != __atomic_load_n(r->sq_tail, __ATOMIC_ACQUIRE)) {
        close(wfd);
        r->loop_wake_fd = r->loop_epoll_fd = -1;
        return -1;
    }
    /* Stage-3: register this hub's provided buffer ring for multishot recv.
     * Best-effort -- on failure ms_pool stays NULL and ms_open returns NULL, so
     * the echo falls back to the single-shot Stage-2 path (no hard failure). */
    if (runloom_iouring_loop_ms_enabled())
        runloom_iouring_loop_ms_pbuf_setup(r);
    return wfd;
}

/* Block the calling hub in its ring until >=1 completion or timeout_ns elapses
 * (timeout_ns < 0 = wait forever).  Submits any pending SQEs first, then drains
 * the whole completion batch.  *flags_out accumulates RUNLOOM_LOOP_F_* bits the
 * caller acts on (pump(0) on F_EPOLL; the sub-list is re-drained at the hub
 * loop top regardless, so F_WAKE needs no extra caller action).  Must run on
 * the ring's owning hub thread (DEFER_TASKRUN reaping constraint). */
void runloom_iouring_loop_wait(runloom_iouring_ring_t *r, long long timeout_ns,
                               int *flags_out)
{
    struct io_uring_getevents_arg arg;
    struct __kernel_timespec ts;
    int rc;
    if (r == NULL) return;
    r->loop_waits++;

    /* Submit deferred SQEs (poll re-arms; Stage-2 ops).  Separated from the
     * wait enter below so an EINTR during submit can't be mistaken for a wait
     * timeout -- to_submit is 0 in the steady Stage-1 case so this is free. */
    runloom_iouring_loop_flush(r);

    memset(&arg, 0, sizeof arg);
    if (timeout_ns >= 0) {
        ts.tv_sec  = (int64_t)(timeout_ns / 1000000000LL);
        ts.tv_nsec = (int64_t)(timeout_ns % 1000000000LL);
        arg.ts = (uint64_t)(uintptr_t)&ts;
    }
    /* Wait for >=1 CQE or the timeout.  GETEVENTS also flushes DEFER_TASKRUN
     * task work so completions materialise here.  -ETIME on timeout (normal);
     * any other error just falls through to the drain (process what's there). */
    do {
        rc = sys_io_uring_enter(r->ring_fd, 0, 1,
                                IORING_ENTER_GETEVENTS | IORING_ENTER_EXT_ARG,
                                &arg, sizeof arg);
    } while (rc < 0 && errno == EINTR);

    runloom_iouring_loop_drain(r, flags_out);
}

/* Per-round, NON-blocking service of the ring (the Go findrunnable order:
 * netpoll(0) before the global queue / steal).  Submits every deferred SQE,
 * asks the kernel to post pending completions only when there is reason to
 * believe it has any (CQ non-empty in shared memory, or IORING_SQ_TASKRUN
 * raised -- both plain loads), then drains what is there.  Returns the number
 * of OP completions drained (the sentinel wake/epoll polls are not counted:
 * they wake nothing by themselves, and a re-armed epoll poll on a still-
 * readable fd would otherwise report progress every round and keep the hub
 * out of its idle path, where netpoll deadlines and the stale-arm probe
 * run).  Without this, a hub kept busy by the global queue / steal
 * never reached loop_wait, so its ops sat unsubmitted and its completions
 * unread until it idled -- the loop backend's 4-19x deficit vs epoll on p223.
 * Owner thread only (SQ producer + DEFER_TASKRUN reaper). */
int runloom_iouring_loop_poll(runloom_iouring_ring_t *r, int *flags_out)
{
    unsigned head, tail;
    if (r == NULL) return 0;
    runloom_iouring_loop_flush(r);
    head = __atomic_load_n(r->cq_head, __ATOMIC_RELAXED);
    tail = __atomic_load_n(r->cq_tail, __ATOMIC_ACQUIRE);
    if (head == tail) {
        /* Nothing posted.  Enter only when the kernel says it has deferred
         * task work to run (TASKRUN) or CQEs parked on its overflow list
         * (CQ_OVERFLOW: more completions than the CQ held in one enter; the
         * task-work list is then EMPTY, so TASKRUN alone would leave them
         * there until the hub idles).  Otherwise this poll cost two loads. */
        if (!(__atomic_load_n(r->sq_flags, __ATOMIC_ACQUIRE) &
              (IORING_SQ_TASKRUN | IORING_SQ_CQ_OVERFLOW)))
            return 0;
        (void)sys_io_uring_enter(r->ring_fd, 0, 0, IORING_ENTER_GETEVENTS, NULL, 0);
        head = __atomic_load_n(r->cq_head, __ATOMIC_RELAXED);
        tail = __atomic_load_n(r->cq_tail, __ATOMIC_ACQUIRE);
        if (head == tail) return 0;
    }
    {
        int ops = runloom_iouring_loop_drain(r, flags_out);
        if (ops > 0) r->loop_polls++;
        return ops;
    }
}

/* ---- Stage-2 proactor I/O: recv/send as ring ops (the throughput path) ----
 *
 * The data op runs in the kernel; the fiber parks until the CQE arrives,
 * drained inline by the hub's loop_poll / loop_wait -- no epoll bridge, no
 * readiness round trip.  Submission is DEFERRED (write the SQE, don't enter):
 * the hub's next per-round loop_poll (or its idle loop_wait) submits the
 * whole batch in one io_uring_enter, so the ops of every fiber that parked
 * this round are submitted + reaped together (the gain/liburing batching
 * win).  Ops reach here only when the direct-first syscall in loop_recv /
 * loop_send would block.  The op record lives on the parked fiber's coro stack -- valid
 * across the park, and the kernel reads/writes the fiber's buffer only
 * while it is parked (single-reaper, same-thread), so there is no aliasing.
 * (These all-C echo fibers are not cancellable, so the completion-model
 * buffer-ownership-on-cancel hazard does not arise here.) */

/* Write an op SQE into the SQ WITHOUT entering (deferred).  Bumps inflight so
 * the hub idle path keeps blocking in the ring while ops are outstanding.  If
 * the SQ is full, flush + drain to free slots (single-issuer, same thread). */
static void runloom_iouring_loop_enqueue_op(runloom_iouring_ring_t *r,
                                            struct io_uring_sqe sqe,
                                            runloom_iouring_op_t *op)
{
    unsigned tail, head, idx;
    for (;;) {
        tail = __atomic_load_n(r->sq_tail, __ATOMIC_RELAXED);
        head = __atomic_load_n(r->sq_head, __ATOMIC_ACQUIRE);
        if ((tail - head) < r->sq_entries) break;
        runloom_iouring_loop_flush(r);
        runloom_iouring_loop_drain(r, NULL);
    }
    idx = tail & r->sq_mask;
    r->sqes[idx] = sqe;
    r->sqes[idx].user_data = (uint64_t)(uintptr_t)op;
    r->sq_array[idx] = idx;
    __atomic_store_n(r->sq_tail, tail + 1, __ATOMIC_RELEASE);
    __atomic_add_fetch(&r->inflight, 1, __ATOMIC_ACQ_REL);
    __atomic_add_fetch(&runloom_iouring_inflight_count, 1, __ATOMIC_ACQ_REL);
}

/* Submit `sqe` (deferred) on the CURRENT hub's ring, park the fiber, and
 * return the CQE result (bytes, 0 on EOF, -1 with errno on error).  The ring is
 * looked up here, at submit, never passed in: the fiber may have migrated at
 * any earlier park, and only the hub it runs on now may write this ring's SQ
 * (see "Cross-hub migration" at the top).  EINVAL off a hub or on a hub with no
 * ring; callers check runloom_mn_current_iouring_ring() first and fall back to
 * the readiness path. */
static runloom_iouring_ssize_t runloom_iouring_loop_io(struct io_uring_sqe sqe)
{
    runloom_iouring_op_t op;
    runloom_iouring_ring_t *r = runloom_mn_current_iouring_ring();
    void *hub;
    if (r == NULL) { errno = EINVAL; return -1; }
    hub = runloom_mn_current_hub_opaque();
    if (hub == NULL) { errno = EINVAL; return -1; }
    op.type   = RUNLOOM_IOURING_OP_SINGLE;
    op.hub    = hub;
    op.g      = runloom_mn_tls_current_g();
    op.result = INT32_MIN;
    op.wait   = RUNLOOM_IOURING_WAIT_INFLIGHT;
    op.ring   = r;            /* this hub ring (cancel_g symmetry; these all-C
                              * echo ops aren't cancellable, but keep the field
                              * defined rather than reading back garbage) */
    runloom_iouring_loop_enqueue_op(r, sqe, &op);
    /* Commit to parking.  loop_wait runs on THIS hub thread, so it cannot
     * complete the op mid-call; the CAS wins here and the later loop_drain sees
     * PARKED and wakes us.  (The handshake is kept for the shared contract.) */
    {
        int prev = RUNLOOM_IOURING_WAIT_INFLIGHT;
        if (__atomic_compare_exchange_n(&op.wait, &prev,
                                        RUNLOOM_IOURING_WAIT_PARKED, 0,
                                        __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
            RUNLOOM_IOU_NOTE(RUNLOOM_IOURING_WAIT_INFLIGHT,
                             RUNLOOM_IOURING_WAIT_PARKED);
            runloom_sched_park_current();
            runloom_coro_yield();
        }
    }
    {
        int32_t rr = __atomic_load_n(&op.result, __ATOMIC_ACQUIRE);
        if (rr < 0) { errno = -rr; return -1; }
        return rr;
    }
}

/* STACKWEAVE_IOURING_LOOP_DIRECT (default ON): try the op as a plain
 * non-blocking syscall first and use the ring only when it would block.  The
 * ring's job is then to WAIT with the data copied on completion, not to carry
 * every op: an echo reply's send never blocks, so it costs one syscall and no
 * park, and a recv that finds data does the same.  Deferring every op to the
 * ring instead put a whole scheduling round of latency in front of each one,
 * which the co-located peer paid as an EAGAIN + park (p223: half the client
 * reads).  Set to 0 to route every op through the ring (the old behaviour). */
static int runloom_iouring_loop_direct(void)
{
    static int v = -1;
    int cur = __atomic_load_n(&v, __ATOMIC_RELAXED);
    if (cur < 0) {
        const char *e = getenv("STACKWEAVE_IOURING_LOOP_DIRECT");
        cur = (e != NULL && *e == '0') ? 0 : 1;
        __atomic_store_n(&v, cur, __ATOMIC_RELAXED);
    }
    return cur;
}

runloom_iouring_ssize_t runloom_iouring_loop_recv(int fd, void *buf, size_t n,
                                                  int flags)
{
    struct io_uring_sqe sqe;
    if (runloom_iouring_loop_direct()) {
        ssize_t k;
        do { k = recv(fd, buf, n, flags | MSG_DONTWAIT); }
        while (k < 0 && errno == EINTR);
        if (k >= 0 || (errno != EAGAIN && errno != EWOULDBLOCK))
            return (runloom_iouring_ssize_t)k;
    }
    memset(&sqe, 0, sizeof(sqe));
    sqe.opcode    = IORING_OP_RECV;
    sqe.fd        = fd;
    sqe.addr      = (uintptr_t)buf;
    sqe.len       = (unsigned)n;
    sqe.msg_flags = (uint32_t)flags;
    return runloom_iouring_loop_io(sqe);
}

runloom_iouring_ssize_t runloom_iouring_loop_send(int fd, const void *buf,
                                                  size_t n, int flags)
{
    struct io_uring_sqe sqe;
    if (runloom_iouring_loop_direct()) {
        ssize_t k;
        /* MSG_NOSIGNAL: the ring's SEND never raises SIGPIPE; match it. */
        do { k = send(fd, buf, n, flags | MSG_DONTWAIT | MSG_NOSIGNAL); }
        while (k < 0 && errno == EINTR);
        if (k >= 0 || (errno != EAGAIN && errno != EWOULDBLOCK))
            return (runloom_iouring_ssize_t)k;
    }
    memset(&sqe, 0, sizeof(sqe));
    sqe.opcode    = IORING_OP_SEND;
    sqe.fd        = fd;
    sqe.addr      = (uintptr_t)buf;
    sqe.len       = (unsigned)n;
    sqe.msg_flags = (uint32_t)flags;
    return runloom_iouring_loop_io(sqe);
}

/* Write a hub's wake eventfd to interrupt its ring wait (called by a peer in
 * runloom_mn_hub_submit when the target hub is ring_waiting).  No-op on -1. */
void runloom_iouring_loop_wake(int wake_fd)
{
    if (wake_fd >= 0) {
        uint64_t one = 1;
        ssize_t w = write(wake_fd, &one, sizeof one);
        (void)w;   /* EAGAIN (counter saturated) is harmless: a wake is pending */
    }
}

/* Process total of hub ring waits, folded in from each ring at disarm (so a
 * value read after run() covers the whole session).  A diagnostic: a run with
 * STACKWEAVE_IOURING_LOOP set and this still 0 never used the loop backend. */
static unsigned long long runloom_iouring_loop_waits_total = 0;

unsigned long long runloom_iouring_loop_waits(void)
{
    return __atomic_load_n(&runloom_iouring_loop_waits_total, __ATOMIC_RELAXED);
}

/* Process total of per-round polls that drained >=1 CQE (folded in at disarm):
 * proof that completions were served between fibers, not only at hub idle. */
static unsigned long long runloom_iouring_loop_polls_total = 0;

unsigned long long runloom_iouring_loop_polls(void)
{
    return __atomic_load_n(&runloom_iouring_loop_polls_total, __ATOMIC_RELAXED);
}

/* Process total of buffers returned through the inbox, i.e. finished by a
 * fiber on a hub other than its handle's owner (folded in at ring disarm):
 * proof that multishot fibers migrated while their stream was open. */
static unsigned long long runloom_iouring_loop_ms_remote_returns_total = 0;

unsigned long long runloom_iouring_loop_ms_remote_returns(void)
{
    return __atomic_load_n(&runloom_iouring_loop_ms_remote_returns_total,
                           __ATOMIC_RELAXED);
}

/* The consumer's side of the same hand-off: buffers a fiber finished off its
 * stream's owner hub and posted to the owner's inbox.  Counted as they are
 * posted (an atomic on the already-slow off-owner path), so after run() it
 * equals ms_remote_returns unless a posted buffer never got back to its pool,
 * and a zero says no fiber migrated with a stream open. */
static unsigned long long runloom_iouring_loop_ms_posted_returns_total = 0;

unsigned long long runloom_iouring_loop_ms_posted_returns(void)
{
    return __atomic_load_n(&runloom_iouring_loop_ms_posted_returns_total,
                           __ATOMIC_RELAXED);
}

/* Tear down the loop wake fd (the polls die with the ring at destroy). */
void runloom_iouring_loop_hub_disarm(runloom_iouring_ring_t *r)
{
    if (r == NULL) return;
    __atomic_add_fetch(&runloom_iouring_loop_waits_total, r->loop_waits,
                       __ATOMIC_RELAXED);
    __atomic_add_fetch(&runloom_iouring_loop_polls_total, r->loop_polls,
                       __ATOMIC_RELAXED);
    __atomic_add_fetch(&runloom_iouring_loop_ms_remote_returns_total,
                       r->ms_remote_returns, __ATOMIC_RELAXED);
    r->loop_waits = 0;
    r->loop_polls = 0;
    r->ms_remote_returns = 0;
    runloom_iouring_loop_ms_pbuf_teardown(r);   /* unregister + free the pool */
    if (r->loop_wake_fd >= 0) { close(r->loop_wake_fd); r->loop_wake_fd = -1; }
    r->loop_epoll_fd = -1;
}

/* ---- Stage-3 multishot recv handle (per connection) ----------------------
 * Heap-allocated (opaque void* to the caller in module_io's all-C echo, which
 * lives in a different translation unit).
 *
 * OWNER VS CONSUMER.  A handle is bound to the ring of the hub that opened it
 * (h->ring, the "owner"): its multishot SQE, its CQEs and its buffers all live
 * there, and only the owner's thread may write that ring's SQ or its
 * provided-buffer ring.  The consumer fiber may migrate at any park and read
 * from any hub.  They share the handle like this:
 *   - Delivered chunks: an SPSC queue (q_*).  The owner's loop_drain produces
 *     (loop_ms_on_cqe) and the fiber consumes (ms_recv).  Copying a chunk out
 *     of the owner's buffer is plain shared memory, safe from any thread.
 *   - Buffer returns and re-arms: done directly when the fiber runs on the
 *     owner; otherwise POSTED.  A return goes into a second SPSC queue (ret_*,
 *     fiber produces, owner consumes), a re-arm is a bit, and the handle is
 *     pushed onto the owner ring's inbox list with a kick to the owner hub,
 *     which carries the work out at the top of its next loop turn
 *     (runloom_iouring_loop_inbox_drain).  Only the consumer posts.
 *   - Parking: a waiting-flag handshake (loop_ms_park / loop_ms_wake).  The
 *     owner can deliver a chunk while the fiber is on its way to park, so the
 *     fiber publishes `waiting`, re-checks, and a wake the owner has claimed
 *     is always consumed by exactly one park (a stray wake would resume the
 *     fiber inside some later, unrelated park).
 *   - Close: a hand-off.  ms_close gives the handle to the owner and never
 *     touches it again; the owner returns every buffer it still holds, cancels
 *     an armed SQE, and frees the handle at its terminal CQE (at once if none
 *     is in flight).
 * The pool bounds every queue: a bid is delivered again only after it has been
 * returned, so no handle ever holds more than q_cap (== ms_count) bids across
 * q_*, ret_* and cur_bid together, and neither SPSC queue can overflow. */

/* h->armed */
#define RUNLOOM_LOOP_MS_IDLE      0   /* no multishot SQE in flight */
#define RUNLOOM_LOOP_MS_ARMED     1   /* a multishot SQE is in flight */
#define RUNLOOM_LOOP_MS_REARMING  2   /* re-arm posted, the owner has not run it yet */
/* h->inbox_bits */
#define RUNLOOM_LOOP_MS_QUEUED    0x1 /* on the owner ring's inbox list */
#define RUNLOOM_LOOP_MS_REARM     0x2
#define RUNLOOM_LOOP_MS_CLOSE     0x4

struct runloom_loop_ms {
    runloom_iouring_op_t op;        /* FIRST: SQE user_data; op.type==MULTISHOT */
    runloom_iouring_ring_t *ring;   /* the owner hub's ring */
    void *owner_hub;                /* the owner hub, for the inbox kick */
    int fd;
    int armed;                      /* RUNLOOM_LOOP_MS_IDLE/_ARMED/_REARMING; atomic */
    int eof;                        /* peer closed (a res==0 CQE); atomic */
    int err;                        /* sticky -errno (NOT ENOBUFS / ECANCELED); atomic */
    int closing;                    /* owner-only: closed, free at the terminal CQE */
    /* park handshake */
    int waiting;                    /* 1 while the fiber is parked or parking; atomic */
    runloom_g_t *waiter_g;
    void *waiter_hub;
    /* inbox */
    int inbox_bits;                 /* RUNLOOM_LOOP_MS_QUEUED | _REARM | _CLOSE; atomic */
    runloom_loop_ms_t *inbox_next;  /* link on the owner ring's inbox list */
    /* Delivered-but-unconsumed buffers, owner -> fiber.  Free-running indices:
     * the owner writes q_tail, the fiber writes q_head.  q_bid/q_len/ret_bid
     * point into the one allocation that backs this handle. */
    uint16_t *q_bid;
    uint16_t *q_len;
    unsigned  q_cap;
    unsigned  q_head;
    unsigned  q_tail;
    /* Buffers the fiber finished off the owner hub, fiber -> owner. */
    uint16_t *ret_bid;
    unsigned  ret_head;             /* owner-owned */
    unsigned  ret_tail;             /* fiber-owned */
    /* partially-consumed head buffer (fiber-owned) */
    int      cur_bid;               /* -1 = none */
    unsigned cur_off, cur_len;
};

/* Write the persistent multishot recv SQE into the hub ring SQ (deferred;
 * loop_wait or the inbox drain submits it).  Bumps inflight so the hub keeps
 * reaping while armed.  Marks the handle armed.  OWNER ONLY (SINGLE_ISSUER).
 * Returns 0 (always succeeds: SQ-full is resolved by flush+drain). */
static int runloom_iouring_loop_ms_submit(runloom_loop_ms_t *h)
{
    runloom_iouring_ring_t *r = h->ring;
    unsigned tail, head, idx;
    struct io_uring_sqe *sqe;
    for (;;) {
        tail = __atomic_load_n(r->sq_tail, __ATOMIC_RELAXED);
        head = __atomic_load_n(r->sq_head, __ATOMIC_ACQUIRE);
        if ((tail - head) < r->sq_entries) break;
        runloom_iouring_loop_flush(r);
        runloom_iouring_loop_drain(r, NULL);
    }
    idx = tail & r->sq_mask;
    sqe = &r->sqes[idx];
    memset(sqe, 0, sizeof(*sqe));
    sqe->opcode    = IORING_OP_RECV;
    sqe->fd        = h->fd;
    sqe->ioprio    = IORING_RECV_MULTISHOT;
    sqe->flags     = IOSQE_BUFFER_SELECT;
    sqe->buf_group = RUNLOOM_LOOP_MS_BGID;
    sqe->user_data = (uint64_t)(uintptr_t)&h->op;
    r->sq_array[idx] = idx;
    __atomic_store_n(r->sq_tail, tail + 1, __ATOMIC_RELEASE);
    __atomic_add_fetch(&r->inflight, 1, __ATOMIC_ACQ_REL);
    __atomic_add_fetch(&runloom_iouring_inflight_count, 1, __ATOMIC_ACQ_REL);
    __atomic_store_n(&h->armed, RUNLOOM_LOOP_MS_ARMED, __ATOMIC_RELEASE);
    return 0;
}

/* Cancel the handle's armed multishot SQE by user_data.  OWNER ONLY.  The
 * cancel's own CQE carries the MSCANCEL sentinel and is discarded by
 * loop_drain; the multishot's terminal CQE (no F_MORE) follows. */
static void runloom_loop_ms_submit_cancel(runloom_loop_ms_t *h)
{
    runloom_iouring_ring_t *r = h->ring;
    /* Taken BEFORE the SQ-full loop: its drain may reap this handle's own
     * terminal CQE (the multishot ended on its own), and with h->closing set
     * on_cqe frees h.  The cancel then targets a dead user_data (-ENOENT,
     * discarded), which is harmless, but h must not be read after the loop. */
    const uint64_t target = (uint64_t)(uintptr_t)&h->op;
    unsigned tail, head, idx;
    struct io_uring_sqe *sqe;
    for (;;) {
        tail = __atomic_load_n(r->sq_tail, __ATOMIC_RELAXED);
        head = __atomic_load_n(r->sq_head, __ATOMIC_ACQUIRE);
        if ((tail - head) < r->sq_entries) break;
        runloom_iouring_loop_flush(r);
        runloom_iouring_loop_drain(r, NULL);
    }
    idx = tail & r->sq_mask;
    sqe = &r->sqes[idx];
    memset(sqe, 0, sizeof(*sqe));
    sqe->opcode    = IORING_OP_ASYNC_CANCEL;
    sqe->addr      = target;                         /* cancel by user_data */
    sqe->user_data = RUNLOOM_LOOP_UD_MSCANCEL;       /* drain discards it */
    r->sq_array[idx] = idx;
    __atomic_store_n(r->sq_tail, tail + 1, __ATOMIC_RELEASE);
}

/* Wake the consumer if it is parked or on its way to park.  OWNER side, after
 * publishing a chunk / EOF / error / de-arm.  The SEQ_CST fence pairs with
 * the one in loop_ms_park: either the fiber's re-check sees what was just
 * published, or this sees `waiting` and claims the wake. */
static void runloom_loop_ms_wake(runloom_loop_ms_t *h)
{
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    if (__atomic_load_n(&h->waiting, __ATOMIC_RELAXED) &&
        __atomic_exchange_n(&h->waiting, 0, __ATOMIC_ACQ_REL)) {
        runloom_g_t *g = h->waiter_g;
        void *hub = h->waiter_hub;
        if (hub != NULL) runloom_mn_wake_g(hub, g);
        else             runloom_sched_wake_safe(g);
    }
}

/* Drain handler: a multishot CQE delivered a buffer / EOF / error / de-arm.
 * Runs on the OWNER inside loop_drain, possibly while the consumer runs on
 * another hub (see OWNER VS CONSUMER). */
static void runloom_loop_ms_on_cqe(runloom_loop_ms_t *h, int32_t res,
                                   uint32_t flags)
{
    int more = (flags & IORING_CQE_F_MORE) != 0;
    if (h->closing) {
        /* Handed over by ms_close: nobody consumes any more, so give each late
         * chunk straight back, and free the handle at the terminal CQE (the
         * kernel posts nothing for this user_data after it). */
        if (res > 0 && (flags & IORING_CQE_F_BUFFER))
            runloom_iouring_loop_ms_buf_return(
                h->ring, (unsigned)(flags >> IORING_CQE_BUFFER_SHIFT));
        if (!more) free(h);
        return;
    }
    if (res > 0 && (flags & IORING_CQE_F_BUFFER)) {
        unsigned bid  = (unsigned)(flags >> IORING_CQE_BUFFER_SHIFT);
        unsigned tail = h->q_tail;       /* owner-owned */
        if (tail - __atomic_load_n(&h->q_head, __ATOMIC_ACQUIRE) < h->q_cap) {
            unsigned slot = tail % h->q_cap;
            h->q_bid[slot] = (uint16_t)bid;
            h->q_len[slot] = (uint16_t)res;
            __atomic_store_n(&h->q_tail, tail + 1, __ATOMIC_RELEASE);
        } else {
            /* Should be unreachable (see the pool bound above).  Don't corrupt
             * memory: return the buffer instead of enqueuing (drops the chunk). */
            runloom_iouring_loop_ms_buf_return(h->ring, bid);
        }
    } else if (res == 0) {
        __atomic_store_n(&h->eof, 1, __ATOMIC_RELEASE);   /* orderly peer close */
    } else if (res < 0) {
        int e = -res;
        if (e != ENOBUFS && e != ECANCELED &&
            __atomic_load_n(&h->err, __ATOMIC_RELAXED) == 0)
            __atomic_store_n(&h->err, e, __ATOMIC_RELEASE);   /* real, sticky */
        /* ENOBUFS (pool momentarily empty) / ECANCELED: not sticky; the
         * multishot de-arms and recv re-arms when it next needs data. */
    }
    if (!more)
        __atomic_store_n(&h->armed, RUNLOOM_LOOP_MS_IDLE, __ATOMIC_RELEASE);
    runloom_loop_ms_wake(h);
}

/* Give a finished buffer back to the owner's pool: directly on the owner,
 * else through the ret queue + inbox. */
static void runloom_loop_ms_post(runloom_loop_ms_t *h, int bits);
static void runloom_loop_ms_give_back(runloom_loop_ms_t *h, unsigned bid)
{
    unsigned t;
    if (runloom_mn_current_iouring_ring() == h->ring) {
        runloom_iouring_loop_ms_buf_return(h->ring, bid);
        return;
    }
    t = h->ret_tail;                     /* fiber-owned */
    h->ret_bid[t % h->q_cap] = (uint16_t)bid;
    __atomic_store_n(&h->ret_tail, t + 1, __ATOMIC_RELEASE);
    __atomic_add_fetch(&runloom_iouring_loop_ms_posted_returns_total, 1,
                       __ATOMIC_RELAXED);
    runloom_loop_ms_post(h, 0);
}

/* Ask for the multishot to be re-armed if it has ended: directly on the
 * owner, else posted.  IDLE -> REARMING is the consumer's claim, so a re-arm
 * is requested at most once per de-arm. */
static void runloom_loop_ms_rearm(runloom_loop_ms_t *h)
{
    int idle = RUNLOOM_LOOP_MS_IDLE;
    if (!__atomic_compare_exchange_n(&h->armed, &idle, RUNLOOM_LOOP_MS_REARMING,
                                     0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE))
        return;                          /* armed, or a re-arm is already posted */
    if (runloom_mn_current_iouring_ring() == h->ring)
        runloom_iouring_loop_ms_submit(h);
    else
        runloom_loop_ms_post(h, RUNLOOM_LOOP_MS_REARM);
}

/* Post work to the owner: set `bits` (plus QUEUED) and, if the handle was not
 * already queued, push it onto the owner ring's inbox list (a lock-free LIFO;
 * only the owner removes, and it takes the whole list at once, so there is no
 * ABA) and kick the owner if the list was empty.  CONSUMER ONLY, and after a
 * CLOSE post the handle belongs to the owner: nothing here touches it after
 * the RMW (not queued) or the push (queued). */
static void runloom_loop_ms_post(runloom_loop_ms_t *h, int bits)
{
    runloom_iouring_ring_t *r = h->ring;   /* read first: see above */
    void *owner = h->owner_hub;
    runloom_loop_ms_t *head;
    int old = __atomic_fetch_or(&h->inbox_bits, bits | RUNLOOM_LOOP_MS_QUEUED,
                                __ATOMIC_ACQ_REL);
    if (old & RUNLOOM_LOOP_MS_QUEUED)
        return;                          /* on the list: the owner reads the new bits */
    head = __atomic_load_n(&r->inbox_head, __ATOMIC_RELAXED);
    do {
        h->inbox_next = head;
    } while (!__atomic_compare_exchange_n(&r->inbox_head, &head, h, 1,
                                          __ATOMIC_RELEASE, __ATOMIC_RELAXED));
    if (head == NULL) {
        /* Dekker with hub_main's pre-wait inbox check (ring_waiting=1, fence,
         * runloom_iouring_loop_inbox_pending): either the owner sees the list
         * before it waits, or this kick breaks its ring wait. */
        __atomic_thread_fence(__ATOMIC_SEQ_CST);
        runloom_mn_hub_kick(owner);
    }
}

/* OWNER: return the buffers the fiber finished off-owner. */
static void runloom_loop_ms_take_returns(runloom_loop_ms_t *h)
{
    runloom_iouring_ring_t *r = h->ring;
    unsigned head = h->ret_head;
    unsigned tail = __atomic_load_n(&h->ret_tail, __ATOMIC_ACQUIRE);
    while (head != tail) {
        runloom_iouring_loop_ms_buf_return(r, h->ret_bid[head % h->q_cap]);
        head++;
        r->ms_remote_returns++;
    }
    h->ret_head = head;
}

/* OWNER: take a closed handle.  Return every buffer it still holds, then
 * cancel the armed SQE (the terminal CQE frees it, see on_cqe) or free it now
 * when nothing is in flight (IDLE, or a REARMING never run). */
static void runloom_loop_ms_close_owner(runloom_loop_ms_t *h)
{
    runloom_iouring_ring_t *r = h->ring;
    unsigned head;
    runloom_loop_ms_take_returns(h);
    h->closing = 1;
    if (h->cur_bid >= 0)
        runloom_iouring_loop_ms_buf_return(r, (unsigned)h->cur_bid);
    for (head = h->q_head; head != h->q_tail; head++)
        runloom_iouring_loop_ms_buf_return(r, h->q_bid[head % h->q_cap]);
    h->q_head = h->q_tail;
    if (__atomic_load_n(&h->armed, __ATOMIC_ACQUIRE) == RUNLOOM_LOOP_MS_ARMED)
        runloom_loop_ms_submit_cancel(h);
    else
        free(h);
}

int runloom_iouring_loop_inbox_pending(runloom_iouring_ring_t *r)
{
    return r != NULL && __atomic_load_n(&r->inbox_head, __ATOMIC_RELAXED) != NULL;
}

/* OWNER, at the top of every hub loop turn (and before hub teardown): carry
 * out the work fibers on other hubs posted for this ring's multishot handles.
 * Returns first (they refill the pool a re-arm draws from), then CLOSE or
 * REARM.  Submits what it wrote, so a re-arm or cancel reaches the kernel
 * without waiting for the next ring wait. */
void runloom_iouring_loop_inbox_drain(runloom_iouring_ring_t *r)
{
    runloom_loop_ms_t *h, *next;
    if (!runloom_iouring_loop_inbox_pending(r)) return;
    h = __atomic_exchange_n(&r->inbox_head, (runloom_loop_ms_t *)NULL,
                            __ATOMIC_ACQUIRE);
    for (; h != NULL; h = next) {
        int bits;
        next = h->inbox_next;            /* before the exchange: a re-post relinks */
        bits = __atomic_exchange_n(&h->inbox_bits, 0, __ATOMIC_ACQ_REL);
        if (bits & RUNLOOM_LOOP_MS_CLOSE) {
            runloom_loop_ms_close_owner(h);
            continue;
        }
        runloom_loop_ms_take_returns(h);
        if ((bits & RUNLOOM_LOOP_MS_REARM) &&
            __atomic_load_n(&h->armed, __ATOMIC_ACQUIRE) == RUNLOOM_LOOP_MS_REARMING)
            runloom_iouring_loop_ms_submit(h);
    }
    runloom_iouring_loop_flush(r);
}

/* Open a multishot recv stream on fd on the CURRENT hub's ring, which becomes
 * the handle's owner for its whole life; the fiber itself stays free to
 * migrate.  Returns an opaque handle, or NULL if multishot isn't available
 * here (not on a hub, no ring, no pbuf pool, alloc failure) -- the caller then
 * falls back to single-shot recv.  Arms the persistent SQE immediately. */
void *runloom_iouring_loop_ms_open(int fd)
{
    runloom_loop_ms_t *h = NULL;
    runloom_iouring_ring_t *r = runloom_mn_current_iouring_ring();
    unsigned cap = 0;
    if (r != NULL && r->ms_pool != NULL) {
        cap = r->ms_count;
        h = (runloom_loop_ms_t *)malloc(sizeof(*h) +
                                        (size_t)cap * 3u * sizeof(uint16_t));
    }
    if (h == NULL) {
        __atomic_add_fetch(&runloom_iouring_loop_ms_fallbacks_total, 1,
                           __ATOMIC_RELAXED);
        return NULL;
    }
    __atomic_add_fetch(&runloom_iouring_loop_ms_opens_total, 1, __ATOMIC_RELAXED);
    memset(h, 0, sizeof(*h));
    h->op.type = RUNLOOM_IOURING_OP_MULTISHOT;
    h->op.ring = r;
    h->ring = r;
    h->owner_hub = runloom_mn_current_hub_opaque();
    h->fd = fd;
    h->armed = RUNLOOM_LOOP_MS_IDLE;
    h->q_bid = (uint16_t *)(h + 1);
    h->q_len = h->q_bid + cap;
    h->ret_bid = h->q_len + cap;
    h->q_cap = cap;
    h->cur_bid = -1;
    runloom_iouring_loop_ms_submit(h);   /* we are on the owner */
    return h;
}

/* Is there anything for ms_recv to act on? */
static int runloom_loop_ms_ready(runloom_loop_ms_t *h)
{
    return __atomic_load_n(&h->q_tail, __ATOMIC_ACQUIRE) != h->q_head ||
           __atomic_load_n(&h->eof, __ATOMIC_ACQUIRE) ||
           __atomic_load_n(&h->err, __ATOMIC_ACQUIRE) ||
           __atomic_load_n(&h->armed, __ATOMIC_ACQUIRE) == RUNLOOM_LOOP_MS_IDLE;
}

/* CONSUMER: park until the owner delivers something.  Publishes `waiting`,
 * then re-checks (the owner may be delivering right now, on another hub).  If
 * something is ready and the fiber takes `waiting` back first, no wake is
 * coming and it returns; if the owner already took it, its wake is in flight
 * and the fiber parks to consume exactly that wake. */
static void runloom_loop_ms_park(runloom_loop_ms_t *h)
{
    h->waiter_g   = runloom_mn_tls_current_g();
    h->waiter_hub = runloom_mn_current_hub_opaque();
    __atomic_store_n(&h->waiting, 1, __ATOMIC_RELEASE);
    __atomic_thread_fence(__ATOMIC_SEQ_CST);
    if (runloom_loop_ms_ready(h) &&
        __atomic_exchange_n(&h->waiting, 0, __ATOMIC_ACQ_REL))
        return;
    runloom_sched_park_current();
    runloom_coro_yield();
}

/* Cooperatively read up to n bytes.  Returns bytes (>0), 0 on EOF, or -1 with
 * errno on error.  Serves from buffered data first, parks for more if needed.
 * CONSUMER; may run on any hub. */
runloom_iouring_ssize_t runloom_iouring_loop_ms_recv(void *handle,
                                                     void *buf, size_t n)
{
    runloom_loop_ms_t *h = (runloom_loop_ms_t *)handle;
    runloom_iouring_ring_t *r = h->ring;
    for (;;) {
        /* 1. Serve from the partially-consumed current buffer (the owner's
         *    pool memory; reading it from any hub is safe). */
        if (h->cur_bid >= 0) {
            unsigned avail = h->cur_len - h->cur_off;
            unsigned take  = (avail < n) ? avail : (unsigned)n;
            char *src = (char *)runloom_iouring_loop_ms_buf_addr(
                            r, (unsigned)h->cur_bid);
            if (src != NULL && take > 0)
                memcpy(buf, src + h->cur_off, take);
            h->cur_off += take;
            if (h->cur_off >= h->cur_len) {
                runloom_loop_ms_give_back(h, (unsigned)h->cur_bid);
                h->cur_bid = -1; h->cur_off = h->cur_len = 0;
            }
            if (take > 0) return (runloom_iouring_ssize_t)take;
            /* a zero-length buffer: drop it and keep going */
            continue;
        }
        /* 2. Promote the next delivered buffer to current. */
        {
            unsigned head = h->q_head;       /* fiber-owned */
            if (__atomic_load_n(&h->q_tail, __ATOMIC_ACQUIRE) != head) {
                unsigned slot = head % h->q_cap;
                h->cur_bid = (int)h->q_bid[slot];
                h->cur_len = h->q_len[slot];
                h->cur_off = 0;
                __atomic_store_n(&h->q_head, head + 1, __ATOMIC_RELEASE);
                continue;
            }
        }
        /* 3. Terminal conditions.  The owner publishes a stream's data before
         *    the EOF or error that ends it, so read the flag, then re-check the
         *    queue before trusting it. */
        {
            int eof = __atomic_load_n(&h->eof, __ATOMIC_ACQUIRE);
            int err = __atomic_load_n(&h->err, __ATOMIC_ACQUIRE);
            if (__atomic_load_n(&h->q_tail, __ATOMIC_ACQUIRE) != h->q_head)
                continue;
            if (eof) return 0;
            if (err) { errno = err; return -1; }
        }
        /* 4. Re-arm if the multishot ended (ENOBUFS / kernel ended it). */
        runloom_loop_ms_rearm(h);
        /* 5. Park until the owner delivers a buffer / EOF / error / de-arm. */
        runloom_loop_ms_park(h);
    }
}

/* Close a multishot stream by handing it to its owner (see OWNER VS
 * CONSUMER): the owner returns its buffers, cancels an armed SQE and frees it.
 * Returns at once; do not touch the handle afterwards.  On the owner hub with
 * nothing queued for it, the owner's part runs right here. */
void runloom_iouring_loop_ms_close(void *handle)
{
    runloom_loop_ms_t *h = (runloom_loop_ms_t *)handle;
    if (h == NULL) return;
    /* Not queued => not on the inbox list, and only this fiber posts, so it
     * cannot become queued under us; the owner's drain runs on this thread. */
    if (runloom_mn_current_iouring_ring() == h->ring &&
        __atomic_load_n(&h->inbox_bits, __ATOMIC_ACQUIRE) == 0) {
        runloom_loop_ms_close_owner(h);
        return;
    }
    runloom_loop_ms_post(h, RUNLOOM_LOOP_MS_CLOSE);
}

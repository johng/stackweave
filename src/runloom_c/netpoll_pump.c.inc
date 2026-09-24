/* Part of netpoll.c -- #included into that translation unit, not compiled
 * standalone.  See netpoll.c for the includes/typedefs/file-scope state
 * these definitions depend on. */


#if defined(RUNLOOM_HAVE_EPOLL)
/* Handle one ready epoll event: io_uring global/per-ring eventfds drain their
 * rings, the legacy generic pump-wake eventfd is drained, and a socket fd is
 * dispatched to its parker (or stashed in the pending-wake bitmap if none is
 * linked yet).  Shared by the shared-epoll pump and, under RUNLOOM_PERHUB_EPOLL,
 * by each hub draining its NESTED shared epoll.  Bumps *woke per parker woken. */
static void runloom_epoll_handle_event(int fd, unsigned int events, int *woke)
{
    int mask = 0;
    /* io_uring eventfd (global ring): drain + walk the CQ; not a parker entry. */
    if (runloom_iouring_eventfd_in_epoll >= 0 &&
        fd == runloom_iouring_eventfd_in_epoll) {
        runloom_iouring_drain();
        return;
    }
    /* Generic pump-interrupt eventfd: a cross-thread waker poked it only to break
     * an idle epoll_wait; drain the level (the re-queued g is already ready). */
    if (__atomic_load_n(&runloom_pump_wake_fd, __ATOMIC_ACQUIRE) == fd && fd >= 0) {
        uint64_t v;
        ssize_t r = read(fd, &v, sizeof v);
        (void)r;
        return;
    }
    /* Per-hub iouring rings: dispatch to the matching ring's drain. */
    {
        int ri;
        struct runloom_iouring_ring *match = NULL;
        for (ri = 0; ri < runloom_iouring_ring_count; ri++) {
            if (runloom_iouring_ring_efds[ri] == fd) {
                match = runloom_iouring_ring_ptrs[ri];
                break;
            }
        }
        if (match != NULL) {
            runloom_iouring_ring_drain(match);
            return;
        }
    }
    /* Fold error/hangup into BOTH directions (EPOLLERR/HUP/RDHUP can carry no
     * IN/OUT bit -- a bare RST / half-close); waking both makes every waiter
     * observe the error and guarantees mask != 0 so the pending-bitmap fallback
     * engages.  (See Go's netpoll; mirrors the select branch.) */
    if (events & (EPOLLIN | EPOLLRDHUP | EPOLLERR | EPOLLHUP))
        mask |= RUNLOOM_NETPOLL_READ;
    if (events & (EPOLLOUT | EPOLLERR | EPOLLHUP))
        mask |= RUNLOOM_NETPOLL_WRITE;
    if (runloom_pump_dispatch_event(fd, mask, 0)) {
        (*woke)++;
    } else if (mask != 0) {
        /* No parker linked yet (or already woken once): stash the mask so the
         * next wait_fd consumes it instead of parking forever. */
        runloom_fd_pending_wake_set(fd, mask);
        /* Level-triggered EPOLLOUT with no writer parked would otherwise re-fire
         * on every pump iteration and busy-spin the idle hub at 100% CPU -- a
         * bare cooperative connect() or a short write leaves the socket writable
         * indefinitely.  A genuinely-waiting writer is always served above
         * (dispatch wakes it and returns >0, leaving OUT armed so the level pump
         * re-reports it for any remaining writers); reaching here means nobody
         * wants OUT right now, so drop the arm.  The pending-WRITE bit stashed
         * just above is still honoured: the next wait_fd(WRITE) re-arms OUT and
         * consumes the pending wake at its post-register re-check. */
        if (mask & RUNLOOM_NETPOLL_WRITE) {
            runloom_netpoll_disarm_out(fd);
            /* TOCTOU close (lens stale-cache-vs-kernel, confirmed lost-wake): a
             * WRITE waiter that linked + committed to PARKED in the window
             * between the dispatch-miss above and here would be STRANDED --
             * disarm_out tears OUT down with no parker check (its direction-
             * agnostic sibling release_if_idle has one, but adding it here would
             * wrongly pin OUT for a pure-READ parker), the level re-trigger is
             * gone, and the pending-WRITE bit set just above is never consumed by
             * an already-parked waiter (consume runs only pre-park).  Re-dispatch
             * WRITE now -- the pool lock was released by disarm_out, so
             * dispatch_event can take its per-pool locks: a committed parker is
             * found in by_fd and claimed/woken (single-claim via its commit-CAS,
             * so no double-wake vs the miss above); a waiter that links AFTER
             * this sees the arm cache without WRITE and re-ADDs OUT fresh in its
             * own register(), so LEVEL re-delivers.  A no-op in the common
             * nobody-wants-OUT case. */
            if (runloom_pump_dispatch_event(fd, RUNLOOM_NETPOLL_WRITE, 0))
                (*woke)++;
        }
        /* The same thing for READ, which the WRITE branch above shipped
         * without and which cost soupchan its whole box: level-triggered
         * EPOLLIN with no reader parked re-fires on every pump iteration
         * exactly as EPOLLOUT does.  Measured both ways -- a socket merely
         * holding unread data and a socket whose peer has hung up each pin
         * a core at 100%.  The hangup case is the one that never recovers,
         * because EPOLLIN|EPOLLHUP|EPOLLRDHUP stays asserted forever and no
         * read will clear it, so an fd left open with nobody parked spins
         * for the remaining life of the process.
         *
         * Ordering note: a bare EPOLLHUP/EPOLLERR sets BOTH direction bits
         * in `mask` above, so a hung-up fd takes the WRITE branch first
         * (narrowing the kernel set to IN-only) and then this one, which
         * finds new_mask == 0 and DELs the registration outright.  That is
         * the intended end state for a dead fd and the reason this runs
         * after, not before.  See runloom_netpoll_disarm_in for the
         * race-safety argument and the syscall-cost trade. */
        if (mask & RUNLOOM_NETPOLL_READ) {
            runloom_netpoll_disarm_in(fd);
            /* Same TOCTOU close as the WRITE side: a READ waiter that linked
             * and committed to PARKED between the dispatch-miss above and
             * here would be stranded, since disarm_in tears IN down with no
             * parker check and the pending-READ bit is only consumed
             * pre-park.  Re-dispatch READ now that disarm_in has released
             * the pool lock.  A no-op in the common nobody-wants-IN case. */
            if (runloom_pump_dispatch_event(fd, RUNLOOM_NETPOLL_READ, 0))
                (*woke)++;
        }
    }
}
#endif

int runloom_netpoll_pump(long long timeout_ns)
{
    long long now;
    long long min_deadline = -1;
    int woke = 0;

    if (!__atomic_load_n(&runloom_netpoll_inited, __ATOMIC_ACQUIRE)) {
        if (runloom_netpoll_init() != 0) return -1;
    }

    /* Deterministic simulated-I/O (RUNLOOM_SIM, Slice 2): the sim pump replaces
     * the real poller block below -- it advances the logical clock to the
     * earliest deadline and fires due timeouts, never sleeping on wall time.
     * Idle-path check (this fn is called only when the scheduler is idle), so the
     * cross-TU gate call is free of the recv-after-recv hot path.  Off by default
     * -> production takes the real path exactly as before. */
    if (runloom_sim_enabled()) {
        /* Native mn-sim (MN_SIM_DST_PLAN.md I2, gate [1]): under the armed
         * controlled scheduler, hubs NEVER pump -- readiness flows solely from
         * the census dispatch of the ready ledger.  Routing a hub into the
         * single-thread sim pump below would lazily allocate a WRONG
         * per-thread sched (the probe-P4 silent corruption).  Not a bare
         * return 0: the hub_main:~1085 pump call IS the netpoll-branch hub's
         * bounded idle wait, so returning instantly would busy-spin H hubs on
         * the ctrl mutex; nap detached (STW-safe) like the bare-nanosleep
         * idle path, capped at 500us.  Interior pump(0) calls stay instant.
         * Wall-clock only -- ordering is decided at the census, so this wait
         * stretches nothing but latency.  (kqueue note, mirror-in-step: the
         * wake_pump self-pipe is written but never drained under this gate; a
         * pipe fills at 64KB unlike a Linux eventfd -- Linux-first scope.) */
        /* Keyed on HUB CONTEXT, not just global armed state: `armed` persists
         * between runs on a live pool, so keying the gate on ctrl_armed alone
         * would steal the pump from a MAIN-THREAD rc.run() H=1-sim scenario
         * coexisting with an idle mn pool (its clock would never advance).
         * The corruption this gate prevents is specifically a HUB thread
         * falling into the single-thread sim pump (wrong per-thread sched);
         * a non-hub caller is exactly the legacy plane and keeps it. */
        if (runloom_mn_ctrl_armed() && runloom_mn_current_hub_opaque() != NULL) {
            if (timeout_ns != 0) {
                long long nap_ns = (timeout_ns < 0 || timeout_ns > 500000LL)
                                   ? 500000LL : timeout_ns;
                PyThreadState *saved = PyEval_SaveThread();
                runloom_sleep_ns(nap_ns);
                PyEval_RestoreThread(saved);
            }
            return 0;
        }
        return runloom_netpoll_sim_pump(timeout_ns);
    }

    /* Earliest deadline across every pool.  O(pool_count) instead of
     * the old O(1) global-heap peek -- pool_count is bounded at
     * RUNLOOM_PARKER_POOL_MAX and the per-pool peek is still O(1). */
    min_deadline = runloom_dh_peek_deadline_global();

    now = monotonic_ns();
    if (min_deadline >= 0) {
        long long until = min_deadline - now;
        if (until < 0) until = 0;
        if (timeout_ns < 0 || until < timeout_ns) timeout_ns = until;
    }

#if defined(RUNLOOM_HAVE_EPOLL)
    {
        struct epoll_event evs[64];
        int n;
        /* epoll_wait's timeout is MILLISECOND-granular.  A positive but
         * sub-millisecond timeout (timeout_ns in (0, 1e6)) must round UP to
         * 1 ms, not truncate to 0: truncating to 0 turns the idle pump into a
         * 100% busy-spin of epoll_wait(0) whenever the nearest sleep deadline
         * is under a millisecond away (e.g. the aio keepalive's 2 ms poll plus
         * a burst of asyncio timers keep it sub-ms).  Round up so the OS thread
         * actually sleeps and an fd that becomes ready still wakes it promptly.
         * A genuine non-blocking pump passes timeout_ns == 0 and stays ms 0. */
        int ms = timeout_ns < 0 ? -1 :
                 (timeout_ns > 1000000000LL ? 1000 :
                  (int)((timeout_ns + 999999LL) / 1000000LL));
        /* RUNLOOM_PERHUB_EPOLL: wait on THIS hub's own epoll (its socket fds +
         * its targeted wake eventfd; the shared runloom_epoll_fd is NESTED for
         * the io_uring set).  epool != NULL only on the per-hub path with a live
         * pool epoll; otherwise wait on the shared epoll exactly as before. */
        int wait_fd = runloom_epoll_fd;
        runloom_parker_pool_t *epool = NULL;
        if (runloom_perhub_epoll()) {
            epool = runloom_parker_pool_for_hub(runloom_mn_current_hub_opaque());
            runloom_parker_pool_lock_ensure_inited(epool);
            if (epool->epoll_fd >= 0) wait_fd = epool->epoll_fd;
            else epool = NULL;   /* pool epoll create failed -> shared fallback */
        }
        Py_BEGIN_ALLOW_THREADS
        n = epoll_wait(wait_fd, evs, 64, ms);
        Py_END_ALLOW_THREADS
        if (n < 0) {
            runloom_netpoll_wait_failed(n);   /* EINTR -> retry; else log+backoff */
        } else if (n > 0) {
            int i;
            for (i = 0; i < n; i++) {
                int fd = evs[i].data.fd;
                if (epool != NULL) {
                    /* This hub's TARGETED wake eventfd: drain the level + loop;
                     * the re-queued g is already on the scheduler ready list. */
                    if (fd >= 0 && fd == epool->wake_efd) {
                        uint64_t v;
                        ssize_t r = read(fd, &v, sizeof v);
                        (void)r;
                        /* Dedup: clear AFTER draining so the next
                         * waker writes; SEQ_CST pairs with wake_pump's exchange.
                         * The hub re-checks sub_head at the loop top after this,
                         * closing the Dekker race. */
                        if (runloom_wake_dedup_enabled())
                            __atomic_store_n(&epool->wake_pending, 0, __ATOMIC_SEQ_CST);
                        continue;
                    }
                    /* The NESTED shared epoll fired (io_uring ring eventfds + the
                     * legacy generic wake live in runloom_epoll_fd).  Drain it
                     * NON-BLOCKING and run each event through the same handler;
                     * the shared set never contains itself, so no recursion. */
                    if (fd >= 0 && fd == runloom_epoll_fd) {
                        struct epoll_event sevs[64];
                        int sn, si;
                        sn = epoll_wait(runloom_epoll_fd, sevs, 64, 0);
                        for (si = 0; si < (sn < 0 ? 0 : sn); si++)
                            runloom_epoll_handle_event(sevs[si].data.fd,
                                                       sevs[si].events, &woke);
                        continue;
                    }
                }
                runloom_epoll_handle_event(fd, evs[i].events, &woke);
            }
        }
    }
#elif defined(RUNLOOM_HAVE_KQUEUE)
    {
        /* Larger batch (vs 64): fewer kevent syscalls per pump under a high
         * parked count.  256 * sizeof(struct kevent) ~= 8 KB, fine on the hub
         * OS-thread stack. */
        struct kevent evs[256];
        struct timespec ts;
        struct timespec *tsp = NULL;
        int n;
        int drained = 0;
        /* PER-HUB kqueue: poll THIS hub's own kqueue (the pool wait_fd/register
         * on this thread used).  Each hub draining its own kqueue removes the
         * shared-kqueue kq_lock contention; dispatch_event still searches every
         * pool's by_fd, so a delivered fd resolves to its parker regardless.
         * hub_main also calls this with timeout 0 PERIODICALLY while busy, so a
         * hub that never idles still drains its own events (load balancing). */
        runloom_parker_pool_t *kpool =
            runloom_parker_pool_for_hub(runloom_mn_current_hub_opaque());
        runloom_parker_pool_lock_ensure_inited(kpool);   /* create backend if first touch */
        if (timeout_ns >= 0) {
            ts.tv_sec = (time_t)(timeout_ns / 1000000000LL);
            ts.tv_nsec = (long)(timeout_ns % 1000000000LL);
            tsp = &ts;
        }
        /* DRAIN-UNTIL-EMPTY: the first kevent honors the caller's timeout
         * (blocking when the hub is idle); once events flow we keep draining
         * NON-BLOCKING until the kqueue empties (a short batch) or a per-pump
         * cap.  Draining only one batch per call left the rest of a hub's ready
         * set waiting for the next 64-turn periodic pump, which throttled
         * wakeup delivery (and thus throughput) under high parked counts -- a
         * 3-hop proxy / many-socket-per-task workload at 50k+ wedged on it.
         * The cap stops a flood from starving g execution.  EV_ONESHOT arm
         * discipline is unchanged, so the kqueue FV model still holds. */
        for (;;) {
            int i;
            /* Fault injection only on the first (blocking) pass, matching the
             * original single-kevent semantics. */
            int finj = (drained == 0)
                       ? runloom_fault_inject(RUNLOOM_FAULT_KQUEUE_WAIT) : 0;
            if (finj) {
                n = -1; errno = finj;      /* short-circuit kevent */
            } else if (kpool->kqueue_fd < 0) {
                /* Backend creation failed (OOM/fd exhaustion): don't spin on a
                 * bad fd -- sleep out the timeout so the hub idles, then re-loop. */
                if (timeout_ns > 0 && drained == 0) runloom_sleep_ns(timeout_ns);
                n = 0;
            } else {
                struct timespec zero = {0, 0};
                /* First pass: caller timeout.  Later passes: non-blocking. */
                struct timespec *passtsp = (drained == 0) ? tsp : &zero;
                Py_BEGIN_ALLOW_THREADS
                n = kevent(kpool->kqueue_fd, NULL, 0, evs, 256, passtsp);
                Py_END_ALLOW_THREADS
            }
            if (n < 0) {
                runloom_netpoll_wait_failed(n);   /* EINTR -> retry; else log+backoff */
                break;
            }
            if (n == 0) break;                    /* timeout / nothing ready */
            for (i = 0; i < n; i++) {
                int fd = (int)evs[i].ident;
                int mask;
                /* Self-pipe wake: carries no parker -- the cross-thread
                 * re-queue already placed the g on the scheduler wake_list,
                 * the byte's only job was to break this idle kevent().
                 * Drain it (EV_CLEAR already reset the edge) and move on. */
                if (kpool->selfpipe_r >= 0 && fd == kpool->selfpipe_r) {
                    char drain[64];
                    while (read(kpool->selfpipe_r, drain, sizeof drain) > 0) { }
                    continue;
                }
                mask = (evs[i].filter == EVFILT_READ) ?
                       RUNLOOM_NETPOLL_READ : RUNLOOM_NETPOLL_WRITE;
                /* Fold EOF/error into BOTH directions, mirroring the epoll
                 * branch above (and Go/libuv/mio).  kqueue arms only the
                 * requested direction one-shot, so a peer RST / half-close
                 * reported as EV_EOF / EV_ERROR on a filter no surviving waiter
                 * is armed on would otherwise be undeliverable -> lost wakeup
                 * (audit finding B1, the strongest teardown-tail-hang
                 * candidate).  Waking both directions makes every waiter on the
                 * dead fd runnable so its next syscall observes the error. */
                if (evs[i].flags & (EV_EOF | EV_ERROR))
                    mask |= RUNLOOM_NETPOLL_READ | RUNLOOM_NETPOLL_WRITE;
                /* wake_all=1: one-shot knote -> wake every same-dir waiter (B2). */
                woke += runloom_pump_dispatch_event(fd, mask, 1);
            }
            drained += n;
            /* Stop when the batch was short (kqueue drained) or the per-pump
             * cap is hit (don't let a flood starve g execution). */
            if (n < 256 || drained >= 4096) break;
        }
    }
#else
    /* POSIX select() backend.  Used by Solaris/illumos and by any host
     * built with RUNLOOM_NETPOLL=select. */
    {
        fd_set rfds, wfds;
        int max_fd = -1;
        int rc = 0;
        struct timeval tv, *tvp = NULL;
        runloom_parked_t *p;

        FD_ZERO(&rfds); FD_ZERO(&wfds);
        /* Build the fd set under the lock, then RELEASE it before the
         * (possibly long) select().  Holding runloom_pool.lock across the
         * GIL release below would invert the GIL<->lock order: another
         * thread that takes the GIL then blocks on runloom_pool.lock would
         * wedge against this thread, which holds the lock and needs the
         * GIL back at Py_END_ALLOW_THREADS.  epoll/kqueue likewise don't
         * hold the lock across their wait. */
        RUNLOOM_RLOCK(&runloom_pool.lock, RUNLOOM_RANK_PARKER_POOL);
        for (p = runloom_pool.head; p != NULL; p = p->next) {
            if (p->fd > max_fd) max_fd = p->fd;
            if (p->events & RUNLOOM_NETPOLL_READ)  FD_SET(p->fd, &rfds);
            if (p->events & RUNLOOM_NETPOLL_WRITE) FD_SET(p->fd, &wfds);
        }
        RUNLOOM_RUNLOCK(&runloom_pool.lock, RUNLOOM_RANK_PARKER_POOL);
        /* Self-pipe read end: a cross-thread runloom_netpoll_wake_pump (a
         * blocking-offload worker re-queueing a g, or any waker that
         * re-queues via the scheduler lists rather than a netpoll fd
         * event) breaks an idle select even with no socket fd parked.
         * It also keeps max_fd >= 0 so the pump blocks-and-is-wakeable
         * instead of spin-returning when the only outstanding work is
         * off-netpoll -- what wedged the single-thread scheduler here. */
        if (runloom_selfpipe_r >= 0) {
            FD_SET(runloom_selfpipe_r, &rfds);
            if (runloom_selfpipe_r > max_fd) max_fd = runloom_selfpipe_r;
        }
        if (timeout_ns >= 0) {
            tv.tv_sec  = (long)(timeout_ns / 1000000000LL);
            tv.tv_usec = (long)((timeout_ns % 1000000000LL) / 1000LL);
            tvp = &tv;
        }
        if (max_fd >= 0) {
            /* Release the GIL across select() exactly like epoll_wait /
             * kevent; otherwise the single-thread scheduler holds the GIL
             * during an idle wait and the blocking-offload workers (which
             * need the GIL to run their Python callable) can never finish
             * to wake it -> deadlock. */
            int finj = runloom_fault_inject(RUNLOOM_FAULT_SELECT);
            if (finj) {
                rc = -1; errno = finj;        /* test-only: short-circuit select() */
            } else {
                Py_BEGIN_ALLOW_THREADS
                rc = select(max_fd + 1, &rfds, &wfds, NULL, tvp);
                Py_END_ALLOW_THREADS
            }
        }
        if (rc > 0) {
            /* Drain the self-pipe if it fired; it carries no parker, the
             * re-queued g is already on the scheduler wake_list. */
            if (runloom_selfpipe_r >= 0 && FD_ISSET(runloom_selfpipe_r, &rfds)) {
                char drain[64];
                while (read(runloom_selfpipe_r, drain, sizeof drain) > 0) { }
            }
            /* Re-acquire for dispatch.  The parked list may have changed
             * during the unlocked select; we walk the CURRENT list and
             * test the snapshot fd set.  A parker added after the snapshot
             * isn't in the set (woken next cycle); a spurious match just
             * makes the g re-check its fd and re-park -- both tolerated by
             * wait_fd's park/re-check loop. */
            RUNLOOM_RLOCK(&runloom_pool.lock, RUNLOOM_RANK_PARKER_POOL);
            p = runloom_pool.head;
            while (p != NULL) {
                runloom_parked_t *next_p = p->next;
                int mask = 0;
                if (FD_ISSET(p->fd, &rfds)) mask |= RUNLOOM_NETPOLL_READ;
                if (FD_ISSET(p->fd, &wfds)) mask |= RUNLOOM_NETPOLL_WRITE;
                if (mask & p->events) {
                    /* Claim before waking (see runloom_pump_claim). */
                    int cur = runloom_pump_claim(p);
                    if (cur != RUNLOOM_PARK_WOKEN) {
                        *(p->ready_out) = mask & p->events;
                        runloom_parker_unlink(&runloom_pool, p);
                        if (cur == RUNLOOM_PARK_PARKED)
                            runloom_mn_wake_g(p->hub, p->g);
                        woke++;
                    }
                }
                p = next_p;
            }
            RUNLOOM_RUNLOCK(&runloom_pool.lock, RUNLOOM_RANK_PARKER_POOL);
        } else if (rc < 0) {
            /* Persistent select() error (e.g. EBADF: a parked fd closed under
             * us, a teardown race) must back off, not busy-spin the idle pump
             * -- the same contract the epoll/kqueue paths get.  EINTR is
             * retried (no backoff).  runloom_netpoll_wait_failed was previously
             * gated to epoll/kqueue and never compiled on a select build, so
             * this path used to peg a CPU on a hard select() error. */
            runloom_netpoll_wait_failed(rc);
        }
    }
#endif

    /* Handle timeouts across every pool: parkers whose deadline has
     * passed get ready=0.  Heap pop per pool while top <= now;
     * O(pool_count + K log K) where K is expired-this-pass. */
    now = monotonic_ns();
    woke += runloom_pump_drain_expired(now);
    return woke;
}

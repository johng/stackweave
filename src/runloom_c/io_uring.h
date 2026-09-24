/* io_uring.h -- public interface to runloom's io_uring backend.
 *
 * On Linux 5.1+ these provide cooperative file I/O via the kernel's
 * io_uring interface.  On other OSes (or older kernels), runloom_iouring_
 * available() returns 0 and the read/write entry points return -1 with
 * errno=ENOSYS; callers should fall back to the thread-pool path.
 *
 * Cooperative model:
 *   - The caller submits one SQE referencing a per-op record on its
 *     own C stack.  user_data on the SQE is the op record pointer.
 *   - The caller parks via runloom_sched_park_safe (single-thread sched).
 *   - The kernel signals an eventfd registered with the ring on each
 *     CQE post.  The eventfd lives in runloom_iouring_eventfd(); the
 *     netpoll pump epoll-registers it and calls runloom_iouring_drain()
 *     when the eventfd fires.
 *   - Drain walks the CQ ring, writes result into each op record, and
 *     wakes the parked fiber via runloom_sched_wake_safe.
 *
 *   Callers running inside an M:N hub take a synchronous spin-drain
 *   path instead -- the eventfd integration is wired into the global
 *   netpoll pump only, and the hub doesn't share that pump.
 */
#ifndef RUNLOOM_IOURING_H
#define RUNLOOM_IOURING_H

#include <stddef.h>
#include <stdint.h>

/* Fixed-width signed ssize_t / off_t equivalents for the ring API (the
 * non-Linux stubs compile against the same header). */
typedef int64_t runloom_iouring_ssize_t;
typedef int64_t runloom_iouring_off_t;

/* 1 if io_uring is available on this system, 0 otherwise.  Lazy-
 * initialises the ring on the first call. */
int runloom_iouring_available(void);

/* Eventfd registered with the ring.  Returns -1 if io_uring is
 * unavailable.  Callers epoll-add this fd (EPOLLIN | EPOLLET) and
 * call runloom_iouring_drain() when it fires. */
int runloom_iouring_eventfd(void);

/* Walk the CQ ring, write results into per-op records, wake parked
 * fibers.  Idempotent; safe to call when no completions are
 * pending. */
void runloom_iouring_drain(void);

/* Number of submitted ops that have not yet been drained.  Used by
 * the scheduler drain loop so it doesn't exit while a fiber is
 * parked waiting for a CQE.  Includes ops in hub-spin-drain. */
int runloom_iouring_inflight(void);

/* In-fiber signal delivery for a fiber parked on an io_uring completion, the
 * analogue of runloom_netpoll_signal_wake.  has_sigwaiter() reports whether
 * signal_wake() would find a taker, so the drain can ask "can I deliver this?"
 * BEFORE running PyErr_CheckSignals -- which consumes the pending signal and
 * cannot be undone (see the gate in runloom_sched_drain.c.inc).
 *
 * signal_wake() had no declaration and no caller until this was added: its own
 * comment claimed "the single-thread drain calls it after netpoll_signal_wake
 * finds no parker", and `git log -S` says it never did.  A fiber blocked on a
 * CQE could not receive a signal at all. */
/* PyObject* -- this header is included where Python.h may not be, so the
 * struct is forward-declared at FILE scope: naming it only inside the
 * prototype would declare a fresh type scoped to that prototype, which
 * then conflicts with the real PyObject at the definition. */
struct _object;
int runloom_iouring_signal_wake(struct _object *exc);
int runloom_iouring_has_sigwaiter(void);

/* Cancel a fiber parked on a single (global-ring) io_uring op: submit an
 * ASYNC_CANCEL so the kernel completes it -ECANCELED and the drain wakes the
 * fiber.  Returns 1 if a cancel was submitted, 0 otherwise (not parked on a
 * cancellable op).  Forward-declared g to avoid a runloom_sched.h include cycle. */
struct runloom_g;
int runloom_iouring_cancel_g(struct runloom_g *g);

/* Submit a pread, park the calling fiber cooperatively, return
 * bytes read or -1 with errno set. */
runloom_iouring_ssize_t runloom_iouring_pread(int fd, void *buf, size_t n,
                                        runloom_iouring_off_t offset);

/* Submit a pwrite, park the calling fiber cooperatively, return
 * bytes written or -1 with errno set. */
runloom_iouring_ssize_t runloom_iouring_pwrite(int fd, const void *buf, size_t n,
                                         runloom_iouring_off_t offset);

#endif

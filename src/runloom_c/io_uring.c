/* io_uring.c -- cooperative io_uring backend.
 *
 * Why we exist: runloom_c.fd_read / fd_write on regular files don't
 * work cooperatively through epoll -- regular file fds always report
 * "ready" so wait_fd is a no-op and the actual read/write blocks the
 * OS thread.  io_uring submits the read/write asynchronously to the
 * kernel; we park the fiber and let other gs run while the kernel
 * processes the op.  When a completion is posted the kernel signals
 * an eventfd registered with the ring; the netpoll pump observes that
 * eventfd (epoll-registered), drains the CQ ring, and wakes the
 * fiber that submitted each op.
 *
 * What we DON'T do: liburing.  Adding a build-time dependency on a
 * native library would compromise runloom's "pip install . just works"
 * story.  We talk to io_uring via the raw syscalls (io_uring_setup,
 * io_uring_enter, io_uring_register) and an mmap'd ring -- about 300
 * lines of code total.
 *
 * Backend availability is runtime-detected via the io_uring_setup
 * syscall returning -ENOSYS on old kernels (<5.1).  In that case
 * runloom_iouring_available() returns 0 and callers fall back to the
 * thread-pool path in monkey.py / runloom.sync.
 *
 * Concurrency model:
 *   - Submission is mutex-protected so multiple OS threads (the global
 *     scheduler thread and any M:N hub thread) can share the single
 *     ring.
 *   - Drain runs lock-free over the CQ ring; wakes are routed via
 *     runloom_sched_wake_safe (global sched g) or runloom_mn_wake_g (hub g)
 *     based on the per-op record's hub pointer.
 *   - The op record lives on the submitter's C stack.  The fiber
 *     doesn't get torn down while parked, so the stack stays alive
 *     through to drain.
 *
 * Hub callers: the eventfd integration is wired into the GLOBAL netpoll
 * pump.  Within an M:N hub there's no shared pump that drains the ring
 * automatically, so hub callers take a synchronous spin-drain path
 * (block in io_uring_enter with min_complete=1 + drain inline).  This
 * regresses the hub case versus single-thread but is correct; future
 * work is one-ring-per-hub for full M:N coverage.
 */
#include "plat.h"

#if defined(__linux__)

#include <errno.h>
#include <fcntl.h>
#include <linux/io_uring.h>
#include <poll.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/eventfd.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

#include "io_uring.h"
#include "mn_sched.h"
#include "netpoll.h"
#include "plat_compat.h"
#include "runloom_lockrank.h"
#include "runloom_sched.h"
#include "runloom_fsm.h"
#include "runloom_io_fsm.h"   /* the total (rc,errno)->event I/O classifier */
#include "runloom_diag.h"     /* runloom_iouwake_trace_event (RUNLOOM_IOUWAKE_TRACE) */

/* IORING_REGISTER_EVENTFD opcode for io_uring_register.  Value is a
 * stable kernel ABI but some older Linux headers don't expose the
 * symbol; define a fallback. */
#ifndef IORING_REGISTER_EVENTFD
#  define IORING_REGISTER_EVENTFD 4
#endif

/* ---------------------------------------------------------------------------
 * io_uring.c is split across the io_uring_*.c.inc fragments below for readability.
 * They are #included here (one translation unit): the fragments share this
 * file's includes, typedefs and file-scope statics and are NOT compiled
 * standalone.  setup.py compiles only io_uring.c.
 * --------------------------------------------------------------------------- */
#include "io_uring_l_sys.c.inc"   /* defines RUNLOOM_IOURING_WAIT_* used below */

/* ---- io_uring SINGLE-op park/wake FSM (OBSERVATIONAL) -----------------------
 * The op->wait commit handshake (INFLIGHT/PARKED/DONE), GenMC-proven in
 * tools/verify/genmc/iouring_waitcommit.c.  A submitter that won't block the OS thread
 * CASes INFLIGHT->PARKED and coro_yields; a concurrent drainer exchanges
 * *->DONE and, iff it observed PARKED, wakes the parker.  Three states:
 *   INFLIGHT -> PARKED : submitter commits to park (CAS).
 *   INFLIGHT -> DONE   : a drainer (often the submitter's own inline drain)
 *                        completes the op before it parks.
 *   PARKED   -> DONE   : a drainer completes a parked op and wakes it.
 * DONE is terminal (the op leaves scope).  This table never drives op->wait
 * (the proven CAS/exchange still do); RUNLOOM_IOU_NOTE() asserts the edge under
 * -DRUNLOOM_FSM_VALIDATE and compiles to nothing otherwise. */
enum {
    RUNLOOM_IOU_EV_PARK = 0,   /* submitter CAS INFLIGHT -> PARKED            */
    RUNLOOM_IOU_EV_DONE,       /* drainer exchange * -> DONE                  */
    RUNLOOM_IOU_EV_COUNT
};
#define RUNLOOM_IOU_STATE_COUNT 3   /* INFLIGHT, PARKED, DONE */

static const signed char runloom_iou_table
        [RUNLOOM_IOU_STATE_COUNT][RUNLOOM_IOU_EV_COUNT]
        __attribute__((unused)) = {
    /*                                  PARK                       DONE */
    [RUNLOOM_IOURING_WAIT_INFLIGHT] = { RUNLOOM_IOURING_WAIT_PARKED, RUNLOOM_IOURING_WAIT_DONE },
    [RUNLOOM_IOURING_WAIT_PARKED]   = { RUNLOOM_FSM_INVALID,         RUNLOOM_IOURING_WAIT_DONE },
    [RUNLOOM_IOURING_WAIT_DONE]     = { RUNLOOM_FSM_INVALID,         RUNLOOM_FSM_INVALID       },
};
RUNLOOM_FSM_ASSERT_TABLE(runloom_iou_table, RUNLOOM_IOU_STATE_COUNT,
                         RUNLOOM_IOU_EV_COUNT, "iouring_wait");
#define RUNLOOM_IOU_NOTE(from, to)                                            \
    RUNLOOM_FSM_NOTE("iouring_wait", runloom_iou_table,                       \
                     RUNLOOM_IOU_STATE_COUNT, RUNLOOM_IOU_EV_COUNT, (from), (to))

#include "io_uring_l_buf.c.inc"
#include "io_uring_l_do.c.inc"
#else  /* !__linux__ */

#include <errno.h>
#include "io_uring.h"

int runloom_iouring_available(void) { return 0; }
int runloom_iouring_eventfd(void)   { return -1; }
void runloom_iouring_drain(void)    { /* no-op */ }
int runloom_iouring_inflight(void)  { return 0; }
int runloom_iouring_signal_wake(struct _object *exc) { (void)exc; return 0; }
int runloom_iouring_has_sigwaiter(void)  { return 0; }
int runloom_iouring_cancel_g(struct runloom_g *g) { (void)g; return 0; }

runloom_iouring_ssize_t runloom_iouring_pread(int fd, void *buf, size_t n, runloom_iouring_off_t offset)
{
    (void)fd; (void)buf; (void)n; (void)offset;
    errno = ENOSYS;
    return -1;
}

runloom_iouring_ssize_t runloom_iouring_pwrite(int fd, const void *buf, size_t n, runloom_iouring_off_t offset)
{
    (void)fd; (void)buf; (void)n; (void)offset;
    errno = ENOSYS;
    return -1;
}

#endif

/* Part of runloom_tcp.c -- #included into that translation unit, not compiled
 * standalone.  Implements the zero-PyObject C entry points declared in
 * runloom_tcp_capi.h.  Mirrors RunloomTCPConn_recv_into / _send_all exactly
 * (same FSM classification, same netpoll park), but without Py_buffer / PyArg /
 * PyLong / PyErr -- so a Cython handler that calls these has an
 * allocation-free, PyObject-free hot loop. */

#include "runloom_tcp_capi.h"

/* recv up to n bytes into buf.  Returns bytes read (0 = EOF), -1 on error. */
Py_ssize_t runloom_tcpconn_c_recv_into(PyObject *conn, void *buf, Py_ssize_t n)
{
    RunloomTCPConn *self = (RunloomTCPConn *)conn;
    int fd;

    if (self->closed || self->fd < 0) { errno = EBADF; return -1; }
    if (n <= 0) return 0;
    fd = self->fd;

    for (;;) {
        long long r = (long long)recv(fd, buf, (size_t)n, 0);
        RUNLOOM_IO_SWITCH(runloom_io_classify(RUNLOOM_IO_RECV, r, errno)) {
        case RUNLOOM_IO_READY:
        case RUNLOOM_IO_EOF:
            return (Py_ssize_t)r;        /* >0 data, or 0 = orderly EOF */
        case RUNLOOM_IO_WOULDBLOCK_R:
        case RUNLOOM_IO_INTR:
            break;                        /* not ready -> park on READ */
        case RUNLOOM_IO_ERROR:
            return -1;                    /* errno already set by recv() */
        case RUNLOOM_IO_PROGRESS:
        case RUNLOOM_IO_WOULDBLOCK_W:
        case RUNLOOM_IO_RETRY:
        case RUNLOOM_IO_EVENT_COUNT:
            runloom_fsm_violation("runloom_tcp_capi.recv_into",
                                  RUNLOOM_IO_RECV, -1, __FILE__, __LINE__);
        } RUNLOOM_IO_SWITCH_END
        if (runloom_netpoll_wait_fd_coop(fd, RUNLOOM_NETPOLL_READ, -1LL) < 0) {
            if (!errno) errno = ECANCELED;
            return -1;
        }
    }
}

/* send exactly n bytes from buf.  Returns n on success, -1 on error. */
Py_ssize_t runloom_tcpconn_c_send_all(PyObject *conn, const void *buf, Py_ssize_t n)
{
    RunloomTCPConn *self = (RunloomTCPConn *)conn;
    int fd;
    Py_ssize_t sent = 0;

    if (self->closed || self->fd < 0) { errno = EBADF; return -1; }
    if (n <= 0) return 0;
    fd = self->fd;

    while (sent < n) {
        long long r = (long long)send(fd, (const char *)buf + sent,
                                      (size_t)(n - sent), 0);
        RUNLOOM_IO_SWITCH(runloom_io_classify(RUNLOOM_IO_SEND, r, errno)) {
        case RUNLOOM_IO_PROGRESS:
            sent += (Py_ssize_t)r;
            continue;                     /* re-check sent < n */
        case RUNLOOM_IO_WOULDBLOCK_W:
        case RUNLOOM_IO_INTR:
            break;                        /* not writable -> park on WRITE */
        case RUNLOOM_IO_ERROR:
            return -1;
        case RUNLOOM_IO_READY:
        case RUNLOOM_IO_EOF:
        case RUNLOOM_IO_WOULDBLOCK_R:
        case RUNLOOM_IO_RETRY:
        case RUNLOOM_IO_EVENT_COUNT:
            runloom_fsm_violation("runloom_tcp_capi.send_all",
                                  RUNLOOM_IO_SEND, -1, __FILE__, __LINE__);
        } RUNLOOM_IO_SWITCH_END
        if (runloom_netpoll_wait_fd_coop(fd, RUNLOOM_NETPOLL_WRITE, -1LL) < 0) {
            if (!errno) errno = ECANCELED;
            return -1;
        }
    }
    return sent;
}

/* ============================================================================
 * Raw-fd, tstate-free I/O for c_entry handlers (no PyObject, no TCPConn, no
 * tstate).  Mirrors the all-C echo (module_io.c.inc runloom_io_c_echo):
 * readiness recv/send + runloom_netpoll_wait_fd.  A Cython cdef handler cimports
 * these (via the __tcp_capi__ capsule) and runs the request loop with zero
 * Python state.
 * ============================================================================ */
Py_ssize_t runloom_tcp_c_fd_recv(int fd, void *buf, Py_ssize_t n)
{
    if (n <= 0) return 0;
    for (;;) {
        long long r = (long long)recv(fd, buf, (size_t)n, 0);
        RUNLOOM_IO_SWITCH(runloom_io_classify(RUNLOOM_IO_RECV, r, errno)) {
        case RUNLOOM_IO_READY:
        case RUNLOOM_IO_EOF:
            return (Py_ssize_t)r;
        case RUNLOOM_IO_WOULDBLOCK_R:
        case RUNLOOM_IO_INTR:
            break;
        case RUNLOOM_IO_ERROR:
            return -1;
        case RUNLOOM_IO_PROGRESS:
        case RUNLOOM_IO_WOULDBLOCK_W:
        case RUNLOOM_IO_RETRY:
        case RUNLOOM_IO_EVENT_COUNT:
            runloom_fsm_violation("runloom_tcp_capi.fd_recv",
                                  RUNLOOM_IO_RECV, -1, __FILE__, __LINE__);
        } RUNLOOM_IO_SWITCH_END
        /* _coop maps the POSITIVE RUNLOOM_NETPOLL_CANCELLED sentinel to -1 so a
         * cancelled park UNWINDS instead of slipping the "< 0" test and busy-
         * spinning on a cancelled-but-open fd under connection churn (matches the
         * TCPConn methods + the cooperative socket fast paths; audit finding B3). */
        if (runloom_netpoll_wait_fd_coop(fd, RUNLOOM_NETPOLL_READ, -1LL) < 0) {
            if (!errno) errno = ECANCELED;
            return -1;
        }
    }
}

Py_ssize_t runloom_tcp_c_fd_send_all(int fd, const void *buf, Py_ssize_t n)
{
    Py_ssize_t sent = 0;
    if (n <= 0) return 0;
    while (sent < n) {
        long long r = (long long)send(fd, (const char *)buf + sent,
                                      (size_t)(n - sent), 0);
        RUNLOOM_IO_SWITCH(runloom_io_classify(RUNLOOM_IO_SEND, r, errno)) {
        case RUNLOOM_IO_PROGRESS:
            sent += (Py_ssize_t)r;
            continue;
        case RUNLOOM_IO_WOULDBLOCK_W:
        case RUNLOOM_IO_INTR:
            break;
        case RUNLOOM_IO_ERROR:
            return -1;
        case RUNLOOM_IO_READY:
        case RUNLOOM_IO_EOF:
        case RUNLOOM_IO_WOULDBLOCK_R:
        case RUNLOOM_IO_RETRY:
        case RUNLOOM_IO_EVENT_COUNT:
            runloom_fsm_violation("runloom_tcp_capi.fd_send_all",
                                  RUNLOOM_IO_SEND, -1, __FILE__, __LINE__);
        } RUNLOOM_IO_SWITCH_END
        /* _coop: unwind a cancelled park instead of busy-spinning (see fd_recv). */
        if (runloom_netpoll_wait_fd_coop(fd, RUNLOOM_NETPOLL_WRITE, -1LL) < 0) {
            if (!errno) errno = ECANCELED;
            return -1;
        }
    }
    return sent;
}

void runloom_tcp_c_fd_close(int fd)
{
    runloom_netpoll_release_if_idle(fd);
    close(fd);
}

/* The exported pointer table for the PyCapsule fallback. */
static const RunloomTCPCAPI runloom_tcp_capi_table = {
    runloom_tcpconn_c_recv_into,
    runloom_tcpconn_c_send_all,
    runloom_tcp_c_fd_recv,
    runloom_tcp_c_fd_send_all,
    runloom_tcp_c_fd_close,
};

/* Called once from module init (module_init.c.inc) to attach the capsule. */
int runloom_tcpconn_capi_register(PyObject *module)
{
    PyObject *cap = PyCapsule_New((void *)&runloom_tcp_capi_table,
                                  RUNLOOM_TCP_CAPI_CAPSULE_NAME, NULL);
    if (cap == NULL) return -1;
    if (PyModule_AddObject(module, "__tcp_capi__", cap) < 0) {
        Py_DECREF(cap);
        return -1;
    }
    return 0;
}

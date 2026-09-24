/* runloom_tcp.c -- runloom_c.TCPConn type, the thin C wrapper around a
 * socket that bypasses Python's socket.socket entirely for the hot
 * path.  See runloom_tcp.h for the API surface.
 *
 * Each method's structure is:
 *   1. try the syscall (recv / send / accept / connect)
 *   2. on EAGAIN, park on netpoll via runloom_netpoll_wait_fd
 *   3. loop
 *
 * The netpoll registration is LEVEL-triggered, armed once per direction on
 * epoll (EV_ONESHOT re-armed per park on kqueue) -- see netpoll_register.c.inc.
 * The first wait_fd call on a fd costs one epoll_ctl ADD and every subsequent
 * same-direction call is zero syscalls.
 *
 * recv()/send()/accept4()/connect() run on non-blocking fds.  Buffer pointers
 * stay valid across coro yields because the syscall is synchronous from our
 * side; the actual wait is in netpoll, not in the recv() call.
 */
#define _POSIX_C_SOURCE 200809L

#include "runloom_tcp.h"
#include "plat.h"
#include "plat_compat.h"
#include "netpoll.h"
#include "runloom_blockpool.h"
#include "mn_sched.h"
#include "runloom_sched.h"
#include "runloom_io_fsm.h"   /* the total (rc,errno)->event I/O classifier */

#include <errno.h>
#include <string.h>
#include <stdlib.h>
#include <stdint.h>

typedef struct runloom_tcpconn_s {
    PyObject_HEAD
    int fd;          /* underlying socket fd; -1 if closed */
    int family;      /* AF_INET / AF_INET6 / etc */
    int is_listener; /* True after listen() succeeds */
    int closed;
} RunloomTCPConn;

#include <sys/socket.h>
#include <sys/types.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <netdb.h>
#include <fcntl.h>
#include <unistd.h>
#include <arpa/inet.h>

#define RUNLOOM_NETPOLL_READ  0x1
#define RUNLOOM_NETPOLL_WRITE 0x2

int runloom_netpoll_wait_fd(int fd, int events, long long timeout_ns);
/* Cooperative-socket wait_fd (maps the CANCELLED sentinel to errno=ECANCELED/-1
 * so the socket fast paths raise instead of re-parking on cancel).  Defined once
 * in the netpoll TU (netpoll_wait_fd.c.inc), shared with module_tcp.c.inc so the
 * monkey tcp_recv/send fast paths honour cancel too.  Audit finding B3. */
int runloom_netpoll_wait_fd_coop(int fd, int events, long long timeout_ns);

/* ============================================================
 * Type object  (struct definition is above)
 * ============================================================ */
static PyTypeObject RunloomTCPConnType;

/* ---------------------------------------------------------------------------
 * runloom_tcp.c is split across the runloom_tcp_*.c.inc fragments below for readability.
 * They are #included here (one translation unit): the fragments share this
 * file's includes, typedefs and file-scope statics and are NOT compiled
 * standalone.  setup.py compiles only runloom_tcp.c.
 * --------------------------------------------------------------------------- */
#include "runloom_tcp_helpers.c.inc"
#include "runloom_tcp_conn_io.c.inc"
#include "runloom_tcp_conn_send.c.inc"
#include "runloom_tcp_conn_net.c.inc"
#include "runloom_tcp_capi.c.inc"      /* zero-PyObject C entry points for Cython handlers */
#include "runloom_tcp_type_init.c.inc"

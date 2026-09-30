# io_uring loop backend: what's next

Status as of the migration + per-round-servicing work (fork PR #27): the loop
backend (`STACKWEAVE_IOURING_LOOP=1`, plus `STACKWEAVE_IOURING_MS=1` for
multishot recv) is correct under cross-hub migration and, on the co-located
p223 echo, single-shot is at epoll parity and multishot with a full buffer
pool is ~10% above it at 2-16 hubs. It is opt-in. This file lists the work
that would let it earn a default.

## 1. Fix the two open failures with the flag on

`tests/test_mn_compat_fixes.py::TestTimeContextMN::test_withtimeout_deadline_fires`
and `tests/test_swarm_netpoll_epoll.py::test_stale_arm_probe_heals_under_mn_subprocess`
fail under `STACKWEAVE_IOURING_LOOP=1` and pass with it off. Both sit where
netpoll deadlines / the stale-arm probe meet the ring wait. They predate #27.
Nothing below should land before these do, because every step below widens
the set of runs that hit them.

## 2. Route Python-level sockets through the ring

Today only the all-C paths use the ring for I/O (`stackweave_c.serve`'s echo
and the TCP C API's `runloom_tcp_c_fd_recv` / `send_all`). A Python-level
socket under the loop backend still does readiness I/O: non-blocking
`recv`/`send`, and on `EAGAIN` it registers in the hub's epoll set and parks;
the ring only changes what the hub *blocks in* (a sentinel poll on the epoll
fd), which adds a hop to every wake rather than removing one. So Python
handlers see no gain from the backend yet.

The work: make `TCPConn.recv/send` (and the monkey-patched `socket` where it
reaches the C API) call `runloom_iouring_loop_recv/send`, so a blocked recv
becomes an `IORING_OP_RECV` with the data copied on completion (one fewer
syscall per wake than readiness + retry), then give each connection a
multishot stream so recv needs no syscall at all. The plumbing exists
(`runloom_tcp_capi.c.inc` already routes through `loop_recv/send` when a hub
ring is present); what is missing is the socket-object entry points and a
migration-safe handle lifetime on the Python object. Expect a smaller
headline gain than p223's all-C echo: for Python handlers interpreter time
dominates the syscall pattern.

## 3. Enable the loop backend by default, with a capability probe

Once 1 is done and the full suite matches with the flag on and off:

- probe at hub init: `io_uring_setup` with `SINGLE_ISSUER | DEFER_TASKRUN |
  TASKRUN_FLAG` succeeds (Linux 6.1+), `IORING_REGISTER_PBUF_RING` succeeds
  (5.19+) for multishot, and io_uring is not disabled by policy (seccomp /
  `kernel.io_uring_disabled`); on any failure fall back to the epoll pump
  silently, the way `ring_create` already degrades its flags;
- invert the switch: `STACKWEAVE_IOURING_LOOP=0` opts out;
- CI: the Ubuntu lanes have io_uring, so this is where the flag-on suite
  becomes the default run; macOS is unaffected;
- keep the epoll pump reachable for debugging (the same reason
  `STACKWEAVE_IOURING_LOOP_DIRECT=0` and `_PUMP_ALWAYS` exist).

This should be its own PR after the measurements in 2, so the default change
carries a benchmark on a second workload, not only p223.

## 4. Fire-and-forget sends

Sends that hit `EAGAIN` still park, and multishot's reply send is a direct
syscall per chunk. A `SEND` whose completion only releases the buffer, with
no park, cuts the remaining park per round trip. Worth measuring only if the
multishot lead over epoll needs to be larger than ~10%.

## 5. Re-run the remote-client benchmark

The p223 numbers are co-located clients (client fibers on the same hubs as
the server), which is the worst case for deferred submission and the case
the per-round poll was built for. The backend's original loopback gain came
from remote clients, where hubs idle between requests; that benchmark has not
been re-run since the migration work.

"""net_echo_server_forever.py -- continuous pygo TCP echo SERVER over the internet.

Runs stackweave_c.serve() bound to a PUBLIC interface (default :: / port 7777) so a
remote pygo client (this project's net_echo_forever.py) can soak it OVER THE
INTERNET.  This is the SERVER-side counterpart to net_echo_forever.py: it
exercises pygo's C accept/spawn scaffold + a per-connection handler fiber doing
recv/send netpoll under real WAN conditions (hundreds-of-ms RTT, real connection
churn, resets).  Each handler echoes recv->send verbatim, forever.

Crash-visible by design (see net_echo_server_forever.sh): ONE long-lived
process, NO restart -- a crash must STAY crashed so it is visible.  faulthandler
on; `kill -USR1 <pid>` dumps every fiber-thread's live stack.  The correctness
oracle (byte-exact echo) lives on the CLIENT side; here the heartbeat proves the
server scheduler is still alive and shows the live fiber count.

Env knobs:
  STACKWEAVE_ECHO_BIND     bind address     (default :: -- all v6; "0.0.0.0" for v4)
  STACKWEAVE_ECHO_PORT     listen port      (default 7777)
  STACKWEAVE_ECHO_HUBS     M:N hubs / SO_REUSEPORT acceptors (default 4)
  STACKWEAVE_ECHO_BACKLOG  listen backlog   (default 1024)
  STACKWEAVE_ECHO_REPORT   seconds between heartbeat lines (default 15)
"""
import faulthandler
import os
import signal
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import stackweave
import stackweave_c

BIND = os.environ.get("STACKWEAVE_ECHO_BIND", "::")
PORT = int(os.environ.get("STACKWEAVE_ECHO_PORT", "7777"))
HUBS = int(os.environ.get("STACKWEAVE_ECHO_HUBS", "4"))
BACKLOG = int(os.environ.get("STACKWEAVE_ECHO_BACKLOG", "1024"))
REPORT = float(os.environ.get("STACKWEAVE_ECHO_REPORT", "15"))
CHUNK = 65536
STOP = [False]


def handle(conn):
    # The benchmark tier's handler, verbatim: echo until the peer closes.
    buf = bytearray(CHUNK)
    mv = memoryview(buf)
    try:
        while True:
            n = conn.recv_into(buf)
            if not n:
                break
            conn.send_all(mv[:n])
    except OSError:
        pass


# Stop plumbing.  The SIGTERM/SIGINT handler runs on the MAIN thread and only
# sets STOP; it cannot wake a fiber parked in a plain sched_sleep (a handler that
# does not raise never cuts a sleep short -- CPython's time.sleep has worked the
# same way since PEP 475), so root's old `sleep(3600)` kept the server up for up
# to an hour after `kill`, on every backend.  The handler writes a byte to a
# pipe instead and root parks on it with wait_fd, which readiness wakes at once
# (asyncio's set_wakeup_fd pattern); root then wakes the reporter the same way.
_READ = 1                                   # stackweave_c.wait_fd READ mask
_STOP_PIPE = [None, None]                   # (r, w) root's wake pipe
_REPORT_PIPE = [None, None]                 # (r, w) reporter's wake pipe


def _close_pipe(pair):
    r, w = pair
    stackweave_c.netpoll_unregister(r)
    os.close(r)
    os.close(w)


def reporter(t_start):
    rep_r = _REPORT_PIPE[0]
    while not STOP[0]:
        # Heartbeat interval as a TIMED park on the report pipe: 0 == interval
        # elapsed (print a heartbeat), anything else == root woke us to stop.
        if stackweave_c.wait_fd(rep_r, _READ, int(REPORT * 1000)) != 0:
            break
        try:
            fc = stackweave_c.fiber_count()
        except Exception:                       # noqa: BLE001 - best-effort liveness
            fc = -1
        sys.stderr.write(
            "[net_echo_srv] up={0:.0f}s alive fibers={1} bind={2}:{3}\n".format(
                time.monotonic() - t_start, fc, BIND, PORT))
        sys.stderr.flush()
    _close_pipe(_REPORT_PIPE)


def root():
    t_start = time.monotonic()
    port, listeners = stackweave_c.serve(
        BIND, PORT, handle, acceptors=HUBS, backlog=BACKLOG)
    sys.stderr.write(
        "[net_echo_srv] LISTENING {0}:{1} acceptors={2} backlog={3}\n".format(
            BIND, port, HUBS, BACKLOG))
    sys.stderr.flush()
    stackweave.fiber(lambda: reporter(t_start))
    # serve()'s C accept loops run under the hubs; park root on the stop pipe
    # until the signal handler writes to it (see _STOP_PIPE), then close the
    # listeners so the accept loops (and run()) drain, and wake the reporter.
    stop_r = _STOP_PIPE[0]
    while not STOP[0]:
        stackweave_c.wait_fd(stop_r, _READ)
    for l in listeners:
        l.close()
    os.write(_REPORT_PIPE[1], b"x")
    _close_pipe(_STOP_PIPE)


def request_stop(signum, frame):
    STOP[0] = True
    try:
        os.write(_STOP_PIPE[1], b"x")       # async-signal-safe; wakes root
    except OSError:
        pass


def main():
    faulthandler.enable()
    faulthandler.register(signal.SIGUSR1, all_threads=True)   # kill -USR1 == live stacks
    _STOP_PIPE[:] = os.pipe()
    _REPORT_PIPE[:] = os.pipe()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    sys.stderr.write(
        "[net_echo_srv] START pid={0} bind={1}:{2} hubs={3} backlog={4} -- "
        "continuous, NO restart (a crash stays crashed so it is visible)\n".format(
            os.getpid(), BIND, PORT, HUBS, BACKLOG))
    sys.stderr.flush()
    stackweave.run(HUBS, main_fn=root)


if __name__ == "__main__":
    main()

"""Cooperative os.read/os.write/readv/writev and stdio (input, sys.stdin)."""
from ._base import *  # noqa: F401,F403  (shared foundation)
from .sockets import _netpoll_unregister, _wait_fd_coop  # noqa: F401
import _socket

# ============================================================
# os.read / os.write / os.readv / os.writev / os.readinto
#
# The cooperative versions leave the fd's blocking mode exactly as they found
# it.  O_NONBLOCK lives on the open file DESCRIPTION, not on the fd: dups, a
# child process that inherited the fd, and every other user of a terminal
# (sys.std* on a tty) share it, so setting it -- even for the length of one
# syscall -- makes their blocking reads and writes fail with EAGAIN.  (These
# patches used to set it on every pollable fd and never clear it:
# docs/dev/FT_CPYTHON_LOCK_GAPS.md, finding 13.)  So each call is routed by
# what the fd is, without changing it:
#
#   * already non-blocking (the caller's choice): try the call, park on EAGAIN.
#   * a blocking SOCKET: recv/send with MSG_DONTWAIT on a borrowed raw socket
#     object, a per-call flag that changes nothing shared.  A write keeps a
#     blocking write's contract and sends everything before it returns.  macOS
#     honours MSG_DONTWAIT on recv but not on send (a send on a full buffer
#     blocks), so there a socket write goes the device way below.
#   * a blocking pipe/FIFO: wait until a zero-timeout poll says the call cannot
#     block, then make the plain blocking call.  A pipe read with at least one
#     byte available returns at once, whatever its size; a write of at most
#     PIPE_BUF bytes to a writable pipe always fits (Linux reports writable
#     with a free buffer slot, macOS with >= PIPE_BUF bytes free), so a larger
#     write goes in PIPE_BUF pieces and returns once all of it is written, as
#     the blocking write would.  CPython's own subprocess._communicate relies on
#     the same two facts.  Fibers never race each other between the probe and
#     the call: both run under a mutex striped by the pipe's inode.
#   * a blocking tty or other character device: reads as for a pipe (readable
#     means a read returns now: a whole line in canonical mode, VMIN bytes in
#     raw mode).  A write goes to the pool once the device polls writable: the
#     room a tty write needs is not knowable, and flow control (^S) can stop it.
#   * anything else (regular files, block devices), or an fd the netpoll cannot
#     watch: the blocking call on a pool worker.
#
# The residual cost of not touching the mode: a reader or writer OUTSIDE this
# process's fibers (another process, a plain thread) that drains or fills the
# same pipe in the microseconds between the probe and the call makes that call
# block the hub's OS thread until the pipe has data or room again; and a raw-
# mode tty read with VMIN > 0 and VTIME > 0 can wait up to VTIME for the rest of
# its bytes.  Setting O_NONBLOCK around the call instead would push the cost
# onto every other user of the description.  The price in speed: a large
# write to a blocking pipe costs a probe per PIPE_BUF piece -- on macOS, where
# PIPE_BUF is 512, ~400 MB/s from a fiber against ~3 GB/s for the old
# O_NONBLOCK path (Linux's PIPE_BUF is 4096).  A program that wants the fast
# path makes the fd non-blocking itself.
# ============================================================
_orig_os_read = None
_orig_os_write = None

_PIPE_BUF = getattr(_select_mod, "PIPE_BUF", 512)
_MSG_DONTWAIT = getattr(socket, "MSG_DONTWAIT", 0)
_SOCK_NONBLOCK = getattr(socket, "SOCK_NONBLOCK", 0)
# Whether send() honours MSG_DONTWAIT on a blocking socket.  macOS's does not:
# it blocks once the buffer is full (recv honours it everywhere).
_SEND_DONTWAIT = bool(_MSG_DONTWAIT) and not _IS_DARWIN
_POLL_IN = getattr(_select_mod, "POLLIN", 1)
_POLL_OUT = getattr(_select_mod, "POLLOUT", 4)
_POLL_NVAL = getattr(_select_mod, "POLLNVAL", 32)

# Route of an fd (see the header above).
_FD_POOL, _FD_NONBLOCKING, _FD_SOCKET, _FD_PIPE, _FD_DEVICE = range(5)

# Readers of one pipe (or device) take one stripe mutex across "probe, then
# call", writers another, so two fibers never both see readiness meant for one
# of them.  Striped by inode (dups of one pipe share it) rather than one per
# pipe, so nothing grows with the number of pipes; a hold is a probe and a
# call that cannot block (up to _PIPE_PIECES_PER_HOLD of each for a large pipe
# write), so sharing a stripe costs little.  The mutex is only ever
# try_lock()ed, with a yield between tries, so a waiter never parks on it and
# never blocks its hub.
_IO_STRIPES = 64


def _new_io_stripes():
    return [stackweave_c.Mutex() for _ in range(2 * _IO_STRIPES)]


_io_stripes = _new_io_stripes()


def _io_stripes_after_fork():
    # A stripe held by a fiber of another hub when the process forked stays
    # held in the child, whose copy of that fiber never runs.
    global _io_stripes
    _io_stripes = _new_io_stripes()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_io_stripes_after_fork)


def _stripe_lock(st, ev):
    mu = _io_stripes[((st.st_ino ^ st.st_dev) % _IO_STRIPES)
                     + (_IO_STRIPES if ev == WRITE else 0)]
    while not mu.try_lock():
        stackweave_c.sched_yield()
    return mu


def _fd_route(fd):
    """(route, stat) for a cooperative call on fd; see the header above."""
    try:
        st = _raw_os_fstat(fd)
    except OSError:
        return _FD_POOL, None       # the call itself raises the real error
    m = st.st_mode
    if stat.S_ISSOCK(m):
        route = _FD_SOCKET
    elif stat.S_ISFIFO(m):
        route = _FD_PIPE
    elif stat.S_ISCHR(m):
        route = _FD_DEVICE
    else:
        return _FD_POOL, st
    try:
        if not os.get_blocking(fd):
            return _FD_NONBLOCKING, st
    except OSError:
        return _FD_POOL, st
    return route, st


def _fd_ready_now(fd, ev):
    """Zero-timeout, level-triggered probe: True when a call in direction ev
    would not block now (ready, or hung up / in error / invalid, where the call
    returns or fails at once), False when it would, None when this fd cannot be
    probed here (macOS poll() reports POLLNVAL on ttys and devices).

    Level-triggered on purpose: wait_fd is edge-triggered, and a pipe still
    holding bytes from an edge already consumed would never fire again."""
    try:
        r, w, _x = _raw_select((fd,) if ev == READ else (),
                               (fd,) if ev == WRITE else (), (), 0)
        return bool(r or w)
    except ValueError:
        pass                         # fd >= FD_SETSIZE: poll() takes any fd
    except OSError:
        return True
    if _raw_poll is None:
        return None
    p = _raw_poll()
    try:
        p.register(fd, _POLL_IN if ev == READ else _POLL_OUT)
        res = p.poll(0)
    except (OSError, ValueError):
        return True
    if not res:
        return False
    if res[0][1] & _POLL_NVAL:
        return None
    return True


def _await_ready(fd, ev):
    """Park until _fd_ready_now(fd, ev); False when the fd cannot be watched
    (probe unsupported, or the netpoll refused it): use the pool then."""
    while True:
        ready = _fd_ready_now(fd, ev)
        if ready:
            return True
        if ready is None or _wait_fd_coop(fd, ev) < 0:
            return False


def _nonblocking_call(fd, ev, op, args):
    """The fd is non-blocking already: try, park on EAGAIN, retry.  If the
    netpoll refuses the fd (wait_fd < 0: kqueue rejects some character
    devices), a pool worker waits for it instead of this loop spinning."""
    while True:
        try:
            return op(fd, *args)
        except (BlockingIOError, InterruptedError):
            if _wait_fd_coop(fd, ev) < 0:
                return _blocking_call(_wait_then_call, fd, ev, op, args)


def _block_until_ready(fd, ev):
    """On a pool worker: block until fd is ready in direction ev, by select(),
    or poll() for an fd select() cannot take; if neither can watch it (macOS
    poll() reports POLLNVAL on devices), sleep a little so the caller's retry
    loop polls instead of spinning."""
    try:
        _raw_select((fd,) if ev == READ else (),
                    (fd,) if ev == WRITE else (), (), None)
        return
    except ValueError:
        pass                         # fd >= FD_SETSIZE
    except OSError:
        return                       # the call itself raises the real error
    if _raw_poll is not None:
        p = _raw_poll()
        try:
            p.register(fd, _POLL_IN if ev == READ else _POLL_OUT)
            res = p.poll()
        except (OSError, ValueError):
            return
        if res and not res[0][1] & _POLL_NVAL:
            return
    _raw_time_sleep(0.005)


def _wait_then_call(fd, ev, op, args):
    """Pool-worker form of _nonblocking_call for an fd the netpoll refused:
    the call, blocking the worker until ready between EAGAINs."""
    while True:
        try:
            return op(fd, *args)
        except (BlockingIOError, InterruptedError):
            _block_until_ready(fd, ev)


def _probe_then_call(fd, st, ev, op, args):
    """Blocking pipe/device: make the plain call only once a probe, under the
    fd's stripe, says it cannot block.  Returns the call's result."""
    while True:
        mu = _stripe_lock(st, ev)
        try:
            ready = _fd_ready_now(fd, ev)
            if ready:
                try:
                    return op(fd, *args)
                except BlockingIOError:
                    pass             # someone made it non-blocking meanwhile
        finally:
            mu.unlock()
        if ready is None or _wait_fd_coop(fd, ev) < 0:
            return _blocking_call(op, fd, *args)


def _borrow_socket(fd):
    """A raw _socket.socket on fd that left its mode alone, or None.  Never
    hold one across a park: a dropped fiber's frames would free it attached,
    and its finalizer closes the fd.  Callers detach() it right after the call."""
    if not _MSG_DONTWAIT:
        return None
    if _SOCK_NONBLOCK:
        # A socket object built with a default timeout set would make the fd
        # non-blocking; SOCK_NONBLOCK in `type` makes it skip that.  (type is
        # otherwise unused by recv/send, so SOCK_STREAM is fine for any socket.)
        typ = socket.SOCK_STREAM | _SOCK_NONBLOCK
    elif _socket.getdefaulttimeout() is not None:
        return None
    else:
        typ = socket.SOCK_STREAM
    try:
        return _socket.socket(type=typ, proto=0, fileno=fd)
    except (OSError, ValueError):
        return None


_NO_SOCKET = object()


def _socket_read(fd, how, op, args):
    """A read on a blocking socket fd with MSG_DONTWAIT: recv / recv_into /
    recvmsg_into, parking on EAGAIN.  _NO_SOCKET when no socket can be borrowed."""
    while True:
        s = _borrow_socket(fd)
        if s is None:
            return _NO_SOCKET
        try:
            if how == "read":
                return s.recv(args[0], _MSG_DONTWAIT)
            if how == "readinto":
                return s.recv_into(args[0], 0, _MSG_DONTWAIT)
            return s.recvmsg_into(args[0], 0, _MSG_DONTWAIT)[0]
        except (BlockingIOError, InterruptedError):
            pass
        finally:
            s.detach()
        if _wait_fd_coop(fd, READ) < 0:
            # The netpoll refused the fd: a pool worker makes the blocking call.
            return _blocking_call(op, fd, *args)


def _as_byte_views(buffers):
    return [memoryview(b).cast("B") for b in buffers]


def _advance(views, n):
    """views with the first n bytes dropped."""
    out = []
    for v in views:
        if n >= len(v):
            n -= len(v)
            continue
        out.append(v[n:] if n else v)
        n = 0
    return out


def _take(views, limit):
    """The first `limit` bytes of views, as a list of views."""
    out = []
    for v in views:
        if limit <= 0:
            break
        if len(v) > limit:
            v = v[:limit]
        out.append(v)
        limit -= len(v)
    return out


def _socket_write_all(fd, views):
    """Send every byte of views on a blocking socket fd with MSG_DONTWAIT,
    parking while the send buffer is full; the count, as a blocking send
    returns.  _NO_SOCKET when no socket can be borrowed before any progress."""
    total = sum(len(v) for v in views)
    done = 0
    while True:
        s = _borrow_socket(fd)
        if s is None:
            if done:
                return done + _blocking_call(_send_all_blocking, fd, views)
            return _NO_SOCKET
        n = None
        try:
            if len(views) == 1:
                n = s.send(views[0], _MSG_DONTWAIT)
            else:
                n = s.sendmsg(views, (), _MSG_DONTWAIT)
        except (BlockingIOError, InterruptedError):
            pass
        except OSError:
            if done:
                return done
            raise
        finally:
            s.detach()
        if n is not None:
            done += n
            if done >= total:
                return done
            views = _advance(views, n)
            continue
        if _wait_fd_coop(fd, WRITE) < 0:
            # The netpoll refused the fd: a pool worker writes the rest.
            return done + _blocking_call(_send_all_blocking, fd, views)


def _send_all_blocking(fd, views):
    # Pool-worker fallback for the remainder of a socket write.
    done = 0
    while views:
        n = _orig_os_writev(fd, views) if _orig_os_writev is not None \
            else _orig_os_write(fd, views[0])
        done += n
        views = _advance(views, n)
    return done


# Pieces one stripe hold may write before it lets other writers of the
# stripe in (each piece is a probe and a write that cannot block).
_PIPE_PIECES_PER_HOLD = 64


def _pipe_write_all(fd, st, views):
    """Write every byte of views to a blocking pipe in pieces of at most
    PIPE_BUF, each made only once the pipe polls writable (so it fits); the
    count, as a blocking write returns.  A piece of <= PIPE_BUF is atomic, so a
    write of <= PIPE_BUF stays atomic too."""
    single = len(views) == 1
    if single:
        mv = views[0]
        total = len(mv)
    else:
        total = sum(len(v) for v in views)
    done = 0
    while True:
        mu = _stripe_lock(st, WRITE)
        ready = False
        try:
            for _ in range(_PIPE_PIECES_PER_HOLD):
                ready = _fd_ready_now(fd, WRITE)
                if not ready:
                    break
                try:
                    if single:
                        n = _orig_os_write(fd, mv[done:done + _PIPE_BUF])
                    else:
                        piece = _take(views, _PIPE_BUF)
                        n = (_orig_os_write(fd, piece[0]) if len(piece) == 1
                             else _orig_os_writev(fd, piece))
                except BlockingIOError:
                    ready = False      # someone made it non-blocking meanwhile
                    break
                except OSError:
                    if done:
                        return done
                    raise
                done += n
                if done >= total:
                    return done
                if not single:
                    views = _advance(views, n)
        finally:
            mu.unlock()
        if ready:
            continue                   # a full hold: let the stripe go, go on
        if ready is None or _wait_fd_coop(fd, WRITE) < 0:
            rest = [mv[done:]] if single else views
            return done + _blocking_call(_send_all_blocking, fd, rest)


def _empty_read(how, args):
    """A read asking for no bytes returns at once from the plain call, as the
    blocking one does; it must not wait for the fd to turn readable."""
    try:
        if how == "read":
            return args[0] <= 0
        if how == "readinto":
            return memoryview(args[0]).nbytes == 0
        return all(memoryview(b).nbytes == 0 for b in args[0])
    except (TypeError, ValueError, BufferError):
        return True                 # the plain call raises the stock error


def _coop_read(fd, how, op, args):
    if _empty_read(how, args):
        return op(fd, *args)
    route, st = _fd_route(fd)
    if route == _FD_POOL:
        return _blocking_call(op, fd, *args)
    if route == _FD_NONBLOCKING:
        return _nonblocking_call(fd, READ, op, args)
    if route == _FD_SOCKET:
        r = _socket_read(fd, how, op, args)
        if r is not _NO_SOCKET:
            return r
    return _probe_then_call(fd, st, READ, op, args)


def _coop_write(fd, op, args, views):
    """views: the bytes to write, as byte views; or None when they could not be
    cast (not C-contiguous), so that op raises the stock error."""
    if views is not None and not any(len(v) for v in views):
        # Nothing to write: the plain call returns 0 at once, as the blocking
        # one does; it must not wait for a full pipe to turn writable.
        return op(fd, *args)
    route, st = _fd_route(fd)
    if route == _FD_POOL or views is None:
        return _blocking_call(op, fd, *args)
    if route == _FD_NONBLOCKING:
        return _nonblocking_call(fd, WRITE, op, args)
    if route == _FD_SOCKET:
        if _SEND_DONTWAIT:
            r = _socket_write_all(fd, views)
            if r is not _NO_SOCKET:
                return r
    elif route == _FD_PIPE and (_orig_os_writev is not None or len(views) <= 1):
        return _pipe_write_all(fd, st, views)
    # A device, or a socket where send() ignores MSG_DONTWAIT or no socket
    # object can be borrowed: the whole blocking call on a pool worker once
    # the fd polls writable.
    _await_ready(fd, WRITE)
    return _blocking_call(op, fd, *args)


def _patched_os_read(fd, n):
    if not _in_fiber():
        return _orig_os_read(fd, n)
    return _coop_read(fd, "read", _orig_os_read, (n,))


def _patched_os_write(fd, data):
    if not _in_fiber():
        return _orig_os_write(fd, data)
    try:
        views = [memoryview(data).cast("B")]
    except (TypeError, ValueError, BufferError):
        views = None
    return _coop_write(fd, _orig_os_write, (data,), views)


_orig_os_readv  = None
_orig_os_writev = None


def _patched_os_readv(fd, buffers):
    # Vectored read -- the readv analogue of read: cooperative on pollable
    # fds, pool offload on regular files.
    if not _in_fiber():
        return _orig_os_readv(fd, buffers)
    return _coop_read(fd, "readv", _orig_os_readv, (buffers,))


def _patched_os_writev(fd, buffers):
    if not _in_fiber():
        return _orig_os_writev(fd, buffers)
    try:
        views = _as_byte_views(buffers)
    except (TypeError, ValueError, BufferError):
        views = None
    return _coop_write(fd, _orig_os_writev, (buffers,), views)


_orig_os_readinto = None


def _patched_os_readinto(fd, buffer):
    # os.readinto (3.14+): read into a caller-provided writable buffer, return
    # the byte count -- the read/readv analogue.  CPython 3.14's
    # _pyio.FileIO.readall()/readinto() route through os.readinto (3.13 used
    # os.read), so a buffered stream .read() on a pipe/tty would block the whole
    # hub without this patch.  Cooperative on pollable fds, pool offload on
    # regular files; the parked fiber keeps `buffer` alive (same contract as
    # readv's buffers).
    if not _in_fiber():
        return _orig_os_readinto(fd, buffer)
    return _coop_read(fd, "readinto", _orig_os_readinto, (buffer,))


_orig_os_close = None


def _patched_os_close(fd):
    """Clear the netpoll registration bit for fd before closing so
    that fd reuse re-registers cleanly under the ET register-once
    scheme.  Pipes, sockets-via-fd, ttys all funnel through here."""
    if _netpoll_unregister is not None and fd >= 0:
        _netpoll_unregister(fd)
    return _orig_os_close(fd)


def _patch_os():
    global _orig_os_read, _orig_os_write, _orig_os_close
    global _orig_os_readv, _orig_os_writev, _orig_os_readinto
    _orig_os_read  = os.read
    _orig_os_write = os.write
    _orig_os_close = os.close
    os.read  = _patched_os_read
    os.write = _patched_os_write
    os.close = _patched_os_close
    if hasattr(os, "readv"):
        _orig_os_readv = os.readv
        os.readv = _patched_os_readv
    if hasattr(os, "writev"):
        _orig_os_writev = os.writev
        os.writev = _patched_os_writev
    if hasattr(os, "readinto"):        # 3.14+: buffered .read() routes here
        _orig_os_readinto = os.readinto
        os.readinto = _patched_os_readinto


def _unpatch_os():
    os.read  = _orig_os_read
    os.write = _orig_os_write
    os.close = _orig_os_close
    if _orig_os_readv is not None:
        os.readv = _orig_os_readv
    if _orig_os_writev is not None:
        os.writev = _orig_os_writev
    if _orig_os_readinto is not None:
        os.readinto = _orig_os_readinto



# ============================================================
# stdio (input, sys.stdin)
# ============================================================
_orig_input = None
_orig_stdin_read = None
_orig_stdin_readline = None


def _patched_input(prompt=""):
    if not _in_fiber():
        return _orig_input(prompt)
    if prompt:
        sys.stdout.write(prompt)
        sys.stdout.flush()
    # Offload the blocking read to a pool worker rather than gating on
    # wait_fd.  sys.stdin is a TextIOWrapper over a BufferedReader: an
    # earlier read slurps everything the kernel pipe has to offer into the
    # user-space decode/byte buffers, and wait_fd -- which only sees
    # kernel-level readiness -- is blind to that buffered data.  Parking on
    # wait_fd for a kernel-empty fd would hang while the next line already
    # sits in the io buffer.  The worker's real read returns any buffered
    # line immediately and only truly blocks when the stream is empty,
    # parking just this fiber.
    return offload(_orig_input, "")


def _patched_stdin_read(*args):
    if not _in_fiber():
        return _orig_stdin_read(*args)
    # See _patched_input: wait_fd is blind to data already buffered in the
    # TextIOWrapper, so offload the blocking read instead of parking on it.
    return offload(_orig_stdin_read, *args)


def _patched_stdin_readline(*args):
    if not _in_fiber():
        return _orig_stdin_readline(*args)
    # See _patched_input: wait_fd is blind to data already buffered in the
    # TextIOWrapper, so offload the blocking read instead of parking on it.
    return offload(_orig_stdin_readline, *args)


def _patch_stdio():
    global _orig_input, _orig_stdin_read, _orig_stdin_readline
    _orig_input = builtins.input
    builtins.input = _patched_input
    # sys.stdin is a TextIOWrapper over a BufferedReader: the underlying
    # buffered read may already hold the next line(s) drained from the
    # kernel pipe, so the patched methods offload the blocking read to a
    # pool worker (which honours that buffering) instead of parking on
    # wait_fd, whose kernel-level readiness check is blind to buffered data.
    try:
        _orig_stdin_read     = sys.stdin.read
        _orig_stdin_readline = sys.stdin.readline
        sys.stdin.read     = _patched_stdin_read
        sys.stdin.readline = _patched_stdin_readline
    except (AttributeError, TypeError):
        # Some stdin replacements (pytest capture, IDLE) don't allow this.
        _orig_stdin_read     = None
        _orig_stdin_readline = None


def _unpatch_stdio():
    builtins.input = _orig_input
    if _orig_stdin_read is not None:
        try:
            sys.stdin.read     = _orig_stdin_read
            sys.stdin.readline = _orig_stdin_readline
        except (AttributeError, TypeError):
            pass


# ============================================================
# getpass.getpass -- offload the blocking tty password read
# ============================================================
# Unlike input(), getpass reads from /dev/tty (opened internally, with echo
# turned off via termios), so we can't simply wait_fd on a known stdin fd.  It
# is rare and human-latency-bound, so offload the whole call to a pool worker,
# parking the fiber.  The tty/termios work is thread-agnostic (no
# per-object thread affinity like sqlite), so running it on a worker is fine.
_orig_getpass = None


def _patched_getpass(prompt="Password: ", stream=None):
    if not _in_fiber():
        return _orig_getpass(prompt, stream)
    return offload(_orig_getpass, prompt, stream)


def _patch_getpass():
    global _orig_getpass
    if _orig_getpass is not None:
        return
    import getpass as _gp
    _orig_getpass = _gp.getpass
    _gp.getpass = _patched_getpass


def _unpatch_getpass():
    global _orig_getpass
    if _orig_getpass is None:
        return
    import getpass as _gp
    _gp.getpass = _orig_getpass
    _orig_getpass = None

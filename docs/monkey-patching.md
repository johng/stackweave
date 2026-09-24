# Monkey-patching the stdlib

`stackweave.monkey.patch()` replaces blocking stdlib calls with cooperative
equivalents that park the current fiber instead of blocking the OS
thread.  After the patch, ordinary `socket.recv`, `time.sleep`,
`select.select`, `ssl.read`, file I/O, `subprocess` waits, DNS lookups,
and several `threading` primitives all yield instead of stalling the
whole interpreter.

This means **you can use any synchronous library** -- `requests`,
`pymysql`, stdlib `urllib`, `psycopg2`, plain `socket` code -- and it
becomes cooperative.

## The basic call

```python
import stackweave

stackweave.monkey.patch()                    # patch everything
```

After this:

```python
import socket, time

def worker():
    s = socket.socket()
    s.connect(("example.com", 80))     # cooperative: parks during the TCP handshake
    s.sendall(b"GET / HTTP/1.0\r\n\r\n")
    time.sleep(0.5)                    # cooperative: only this fiber sleeps
    data = s.recv(8192)                # cooperative: parks until data arrives
    s.close()
    return data

stackweave.fiber(worker)
stackweave.run(1)
```

## What gets patched

The patch is divided into **categories** you can selectively
enable/disable:

| Category | What changes |
| --- | --- |
| `socket` | `socket.socket`'s `connect`, `recv`, `send`, `sendall`, `accept`, `recv_into`, `recvfrom`, `sendto` park on `wait_fd` instead of blocking. |
| `time` | `time.sleep` becomes cooperative (uses scheduler's sleep heap). |
| `select` | `select.select`, `select.poll`, `selectors.*` rerouted through stackweave's netpoll. |
| `os` | `os.read`, `os.write` on regular files dispatch to a worker thread (`run_in_executor`-style) so the fiber doesn't block. |
| `ssl` | `ssl.SSLSocket` reads/writes park on `wait_fd`; handshake is cooperative. |
| `subprocess` | `Popen.wait` / `communicate` poll the child's exit cooperatively. |
| `threading` | `threading.Event`, `threading.Lock` (when used inside a fiber) park instead of locking. |
| `queue` | `queue.Queue.get/put` cooperate when called from a fiber. |
| `stdio` | `sys.stdin.readline()` / `input()` park on the underlying fd. |
| `getpass` | `getpass.getpass()` (a blocking `/dev/tty` read) is offloaded to a worker so the fiber parks instead of wedging its hub. |
| `dns` | `socket.getaddrinfo` runs in parallel for A/AAAA records. |

All default to enabled.  Opt out with kwargs:

```python
stackweave.monkey.patch(threading=False, queue=False)
```

## Unpatch

```python
stackweave.monkey.unpatch()              # reverse everything
stackweave.monkey.unpatch(socket=False)  # keep socket patched, reverse the rest
```

Patching is **idempotent** -- calling `patch()` twice does nothing the
second time.  Unpatch is the inverse.

## Recipe: a fully synchronous-looking HTTP fetcher

```python
import stackweave
import urllib.request

stackweave.monkey.patch()

def fetch(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read()

def main():
    urls = [
        "http://example.com",
        "http://example.org",
        "http://example.net",
    ]
    results = stackweave.Chan(len(urls))
    for u in urls:
        stackweave.fiber(lambda url=u: results.send((url, len(fetch(url)))))
    for _ in urls:
        print(results.recv()[0])

stackweave.fiber(main)
stackweave.run(1)
```

Three HTTP requests, fully concurrent, written in completely linear
synchronous style -- `urllib.request` doesn't know it's been
monkey-patched.

## Recipe: a database pool with `pymysql`

```python
import stackweave
import pymysql                                # plain blocking driver

stackweave.monkey.patch()

def query(sql):
    conn = pymysql.connect(host="db", user="x", password="y", db="z")
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.close()

def worker(i):
    rows = query("SELECT id FROM jobs WHERE bucket = %s" % i)
    print("bucket", i, "->", len(rows), "rows")

for i in range(32):
    stackweave.fiber(lambda i=i: worker(i))
stackweave.run(1)
```

32 concurrent MySQL queries on one OS thread, no thread pool, no
`async` rewrite.

## Caveats

### Patch early

```python
import stackweave
stackweave.monkey.patch()        # <-- before importing modules that capture sockets

import some_library        # this sees patched socket from the start
```

If a library does `from socket import socket` and caches the class
at import time, *and* you patch after that import, the library's
cached reference still points at the original.  Some patches rebind
class attributes (so the cached class becomes cooperative), but the
safe ordering is patch-then-import.

### Reentrancy on legacy `selectors`

`selectors.DefaultSelector` is replaced wholesale.  Code that imports
`DefaultSelector` *before* `patch()` and then constructs new instances
still gets the patched version because we rebind the class attribute.

### `threading.Thread` is not replaced

Patching doesn't turn `threading.Thread` into a fiber -- it would
break too many assumptions.  If you spawn an OS thread, it runs
independently of the stackweave scheduler.

For "I want stackweave, not threads," use `stackweave.fiber(fn)` or
`stackweave.sync.fiber(fn)`.

### `os.read` on a regular file dispatches to a thread

The Linux io_uring backend (when available) lets us do truly async
file I/O, but the default path is a small executor that runs the
read/write off the scheduler's OS thread.  This means file I/O won't
*block* your fibers, but it does pay a thread-hop on each call.
See [io_uring](https://github.com/johng/stackweave/blob/main/src/runloom_c/io_uring.c)
for direct ring access.

That executor is the same backend `stackweave.monkey.offload()` uses, and it is
the mechanism described in the next section.

### How offloading works, and where it is going

A blocking call that stackweave cannot make cooperative -- buffered file
`read`/`write`, a C-extension database driver, `socket.gethostbyaddr` (libc,
and it takes no timeout), CPU-bound hashing -- has to run somewhere other than
the fiber's hub, or it stops that hub's scheduler loop.

**Today** that somewhere is a pool of bare OS threads
(`monkey/_base.py`, `_ThreadPoolBackend`): worker threads block in a raw
`_queue.SimpleQueue.get()`, and each submitted task gets a self-pipe from a
parker pool so the calling fiber can park on `wait_fd` until a worker writes a
wake byte. It works, but those workers sit *outside* the scheduler entirely --
no fiber, no deque, no scheduler loop -- so submission, completion and wakeup
are all hand-rolled, and that hand-rolled path is where this subsystem's bugs
have historically lived.

**The replacement**, live now, is offload hubs (see
[API reference](api-reference.md#offload-hubs)): reserve K extra hubs with
`stackweave.run(n, main, offload_hubs=K)`, run the blocking call there as an
ordinary fiber, and let the result come back over a normal channel. Submit, completion and wake then reuse the same scheduler code
every other fiber uses, and there is no completion protocol left to get wrong.

Two consequences worth knowing:

- It needs **no patched CPython**. Nothing migrates between hubs -- the offload
  fiber is born and dies on its hub, the caller never leaves its own -- so the
  cross-hub tstate problem (`stackweave.migration_available()`) does not arise.
- It does **not** raise blocking concurrency. A blocked hub cannot run its
  scheduler loop, so K offload hubs carry K concurrent blocking calls, the same
  arithmetic as the thread pool. The gain is correctness and maintainability,
  not throughput.

`monkey.offload()` routes through offload hubs automatically whenever any are
reserved, and falls back to the thread pool when none are -- so the default
build behaves exactly as before. Reserve them with
`stackweave.run(n, main, offload_hubs=K)` (or `STACKWEAVE_OFFLOAD_HUBS=K`).

The pool is not going away: it is still the only route for a caller outside any
fiber (foreign OS threads must never park a non-existent fiber), for a
single-thread `run(1)`, and for anyone who reserves none.

## Listing applied patches

```python
import stackweave
stackweave.monkey.patch()
print(stackweave.monkey._applied)
# {'socket', 'time', 'select', 'os', 'ssl', 'subprocess',
#  'threading', 'queue', 'stdio', 'dns'}
```

(`_applied` is a private set but stable across versions.)

## When NOT to monkey-patch

If your entire program is written in `async def` and uses
`stackweave.aio.run` for I/O, you don't need monkey-patching -- the asyncio
bridge already drives I/O through stackweave's netpoll.  Monkey-patching is
for **mixing** sync code with the scheduler.

If you're embedding stackweave inside another process that also uses
threads + blocking I/O for unrelated work, don't patch -- confining
stackweave to its own region keeps the rest unaffected.

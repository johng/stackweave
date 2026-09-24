# Quickstart

This page gets you from zero to a running fiber, then through the
core primitives: channels, sleep, fan-out, and a tiny TCP echo server.

## Your first fiber

```python
import stackweave

def hello():
    print("hello from a fiber!")

stackweave.fiber(hello)        # spawn -- doesn't run yet
stackweave.run(1)            # drive the scheduler until everyone finishes
```

Output:

```
hello from a fiber!
```

`go(fn)` queues `fn` for execution on the stackweave scheduler.  Nothing
runs until you call `stackweave.run(1)` -- that's the scheduler's main
loop, equivalent of Go's program-startup runtime.

## Many fibers

```python
import stackweave

def worker(i):
    print("worker", i)

for i in range(10):
    stackweave.fiber(lambda i=i: worker(i))   # bind i per-spawn
stackweave.run(1)
```

Output (order is scheduler-dependent):

```
worker 0
worker 1
worker 2
...
worker 9
```

The `lambda i=i:` is the standard Python late-binding workaround for
closures over a loop variable.  Each fiber captures its own `i`.

## Cooperative sleep

`stackweave.sched_sleep(seconds)` suspends the current fiber for at
least `seconds`, letting other fibers run in the meantime.  This is
not `time.sleep` -- `time.sleep` would block the whole OS thread.

```python
import time, stackweave

def slow():
    print("start", time.time())
    stackweave.sched_sleep(0.5)
    print("end  ", time.time())

# Spawn three sleeps concurrently; they all wake at ~the same time.
for _ in range(3):
    stackweave.fiber(slow)
stackweave.run(1)
```

Output:

```
start 1716901234.001
start 1716901234.001
start 1716901234.001
end   1716901234.503
end   1716901234.503
end   1716901234.503
```

All three sleeps ran in parallel because the scheduler parked each
fiber on a min-heap of wake times.

## Channels

A channel is a typed FIFO that producers `send()` into and consumers
`recv()` from.  Buffered channels hold a fixed number of values;
unbuffered channels rendezvous (the sender blocks until a receiver is
ready, and vice-versa).

```python
import stackweave

ch = stackweave.Chan(10)            # buffered, capacity 10

def producer():
    for i in range(5):
        ch.send(i)
    ch.close()

def consumer():
    for v in ch:                   # iterates until ch closes
        print("got", v)

stackweave.fiber(producer)
stackweave.fiber(consumer)
stackweave.run(1)
```

Output:

```
got 0
got 1
got 2
got 3
got 4
```

See [Channels](channels.md) for the full API -- `try_send`/`try_recv`,
`select`, error semantics on closed channels.

## TCP echo server

The simplest realistic program -- a TCP server that echoes whatever
clients send.

```python
import socket
import stackweave

stackweave.monkey.patch()                 # makes socket cooperative

def handle(conn):
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                return
            conn.sendall(data)
    finally:
        conn.close()

def accept_loop():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 9000))
    srv.listen(128)
    while True:
        conn, _ = srv.accept()
        stackweave.fiber(lambda c=conn: handle(c))

stackweave.fiber(accept_loop)
stackweave.run(1)
```

Test it:

```bash
echo "hi" | nc 127.0.0.1 9000
```

The `stackweave.monkey.patch()` call swaps `socket.socket`'s methods for
cooperative versions that park on `wait_fd` instead of blocking the OS
thread.  See [Monkey-patching](monkey-patching.md) for the full list.

## Already have `async def` code?

Run it on the stackweave scheduler with `stackweave.aio`:

```python
import asyncio
import stackweave

async def handler(reader, writer):
    line = await reader.readline()
    writer.write(b"echo: " + line)
    await writer.drain()
    writer.close()

async def main():
    server = await stackweave.aio.start_server(handler, "127.0.0.1", 9000)
    async with server:
        await server.serve_forever()

stackweave.aio.run(main())
```

`stackweave.aio.run(coro)` is the equivalent of `asyncio.run(coro)`.  It
installs the stackweave event-loop policy, creates a `StackweaveEventLoop`, and
drives your top-level coroutine.  See [Asyncio bridge](asyncio.md) for
when this wins vs. losing against vanilla asyncio.

## Where to go next

- [Channels](channels.md) -- the deep dive on send/recv/select
- [Monkey-patching](monkey-patching.md) -- making the stdlib cooperative
- [Cookbook](cookbook.md) -- patterns: worker pool, pipeline, fan-in/out
- [Stack sizing](stack-sizing.md) -- keep memory low under load

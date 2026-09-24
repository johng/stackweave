"""Adversarial QA: the stackweave.aio asyncio bridge.

The aio bridge is where most of the recent compat bugs lived; CLAUDE.md lists a
dozen fragile invariants.  We target the observable ones:

  * connection_made-that-writes -- a server greeting written inside
    connection_made must reach the client (the _io_g seed-before-callback bug);
  * server close() wakes its accept-loop fibers -- repeated create/close
    must not accumulate parked fibers (the per-server leak);
  * cancellation -- a task parked in sleep / I/O / executor must take a
    CancelledError, not hang;
  * SLOW RETURN -- wait_for must time out promptly and overlap, gather must run
    concurrently (not serialise);
  * _driver sends None -- a custom awaitable whose __await__ yields a plain
    iterator (no .send) must not raise "object has no attribute 'send'".

Driven through stackweave.aio.run() (its asyncio.run drop-in), no pytest-asyncio.
"""
import asyncio
import os
import socket
import sys
import time

import pytest

import stackweave.aio as aio
import stackweave_c as rc
from adv_util import hang_guard, assert_faster_than, RealBarrier as _RealBarrier


def _parked():
    return int(rc.stats().get("netpoll_parked_self", rc.stats().get("netpoll_parked", 0)))


# --------------------------------------------------------------------------
# connection_made that writes (the _io_g seed invariant)
# --------------------------------------------------------------------------
def test_connection_made_write_reaches_client():
    got = {}
    async def body():
        loop = asyncio.get_event_loop()
        class Srv(asyncio.Protocol):
            def connection_made(self, tr):
                tr.write(b"GREETING")          # write INSIDE connection_made
        class Cli(asyncio.Protocol):
            def connection_made(self, tr):
                self.tr = tr
            def data_received(self, data):
                got["greeting"] = data
                self.tr.close()
        server = await loop.create_server(Srv, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        await loop.create_connection(Cli, "127.0.0.1", port)
        await asyncio.sleep(0.05)
        server.close()
        await server.wait_closed()
    with hang_guard(20, "connection_made write"):
        aio.run(body())
    assert got.get("greeting") == b"GREETING"


# --------------------------------------------------------------------------
# server close() wakes accept loops -- no parked-fiber accumulation
# --------------------------------------------------------------------------
def test_server_close_does_not_leak_accept_fibers():
    async def cycle():
        loop = asyncio.get_event_loop()
        server = await loop.create_server(asyncio.Protocol, "127.0.0.1", 0)
        server.close()
        await server.wait_closed()
    def one():
        aio.run(cycle())
    with hang_guard(30, "server close no leak"):
        one()                              # warm up (loop/keepalive setup)
        base = _parked()
        for _ in range(10):
            one()
        after = _parked()
    assert after <= base, "accept-loop fibers leaked: parked %d -> %d" % (base, after)


# --------------------------------------------------------------------------
# cancellation: parked task must take CancelledError, not hang
# --------------------------------------------------------------------------
def test_cancel_task_parked_in_sleep():
    async def body():
        async def victim():
            await asyncio.sleep(100)
            return "finished"
        t = asyncio.ensure_future(victim())
        await asyncio.sleep(0.02)
        t.cancel()
        try:
            await t
            return "not-cancelled"
        except asyncio.CancelledError:
            return "cancelled"
    with hang_guard(20, "cancel parked sleep"):
        assert aio.run(body()) == "cancelled"


def test_cancel_task_parked_in_socket_recv():
    async def body():
        loop = asyncio.get_event_loop()
        a, b = socket.socketpair()
        a.setblocking(False)
        async def victim():
            await loop.sock_recv(a, 64)        # nobody sends -> parks
            return "got-data"
        t = asyncio.ensure_future(victim())
        await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
            out = "not-cancelled"
        except asyncio.CancelledError:
            out = "cancelled"
        a.close(); b.close()
        return out
    with hang_guard(20, "cancel parked recv"):
        assert aio.run(body()) == "cancelled"


# --------------------------------------------------------------------------
# slow return: wait_for timeout + gather concurrency
# --------------------------------------------------------------------------
def test_wait_for_times_out_promptly_and_cancels_inner():
    inner_cancelled = {}
    async def body():
        async def slow():
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                inner_cancelled["yes"] = True
                raise
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(slow(), timeout=0.05)
            return ("no-timeout", 0)
        except asyncio.TimeoutError:
            return ("timeout", time.monotonic() - t0)
    with hang_guard(20, "wait_for timeout"):
        outcome, el = aio.run(body())
    assert outcome == "timeout"
    assert el < 1.0, "wait_for took %.3fs for a 50ms timeout (slow return)" % el


def test_gather_runs_concurrently_not_serial():
    # Peak concurrency, not a wall clock.  `el < 0.3` for 8x50ms is a statement
    # about the machine, and macOS CI missed it at 0.337s while the coroutines
    # ran perfectly concurrently.  Serialised execution can never put two of
    # these intervals in flight at once, however fast the box is.
    spans = []

    async def body():
        async def unit(i):
            t_in = time.monotonic()
            await asyncio.sleep(0.05)
            spans.append((t_in, time.monotonic()))
            return i
        return await asyncio.gather(*[unit(i) for i in range(8)])
    with hang_guard(20, "gather concurrency"):
        res = aio.run(body())
    assert res == list(range(8))
    assert len(spans) == 8, "every unit should have reported (%r)" % (spans,)
    events = []
    for st, en in spans:
        events.append((st, 1))
        events.append((en, -1))
    events.sort(key=lambda ev: (ev[0], ev[1]))   # END before START on a tie
    cur = peak = 0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    assert peak >= 2, "gather serialised: peak concurrency %d (%r)" % (peak, spans)


# --------------------------------------------------------------------------
# run_in_executor offload (+ that a parked executor call overlaps)
# --------------------------------------------------------------------------
def test_run_in_executor_offload_and_overlap():
    spans = []                              # (start, end) per offload

    # OVERLAP IS REQUIRED, NOT MEASURED.  Two earlier forms of this assertion
    # both measured a wall clock and both flaked: `el < 0.4` (macos-14 saw
    # 0.472s while the offloads really did overlap), then peak concurrency over
    # two 50ms spans -- which is machine-speed independent in principle, but
    # still needs BOTH threads scheduled inside the same 50ms, and a loaded
    # runner may simply not do that (macOS reported peak 1; reproduced here 2/15
    # under 4x CPU oversubscription even after pre-warming the pool).
    #
    # A rendezvous removes the timing question entirely: neither offload can
    # leave `blocking` until the other has entered it, so passing the barrier IS
    # concurrent execution, at any speed.  If the scheduler serialises them the
    # first one waits alone and the barrier times out, which is the failure we
    # want to catch -- reported as BrokenBarrierError rather than a number that
    # needs interpreting.  The barrier is the pre-patch class because these run
    # on genuine executor threads a cooperative one would never release.
    barrier = _RealBarrier(2, timeout=20)

    async def body():
        loop = asyncio.get_event_loop()
        def blocking(x):
            t_in = time.monotonic()
            barrier.wait()                 # both offloads must be in flight
            spans.append((t_in, time.monotonic()))
            return x * 2
        t0 = time.monotonic()
        # two offloads should overlap on the pool, not serialise
        a, b = await asyncio.gather(
            loop.run_in_executor(None, blocking, 10),
            loop.run_in_executor(None, blocking, 20),
        )
        return (a, b), time.monotonic() - t0
    with hang_guard(20, "run_in_executor"):
        (a, b), el = aio.run(body())
    assert (a, b) == (20, 40)
    # Reaching here at all means both offloads cleared the rendezvous, so they
    # were concurrent by construction; serialisation raises BrokenBarrierError
    # out of aio.run above.  The spans are kept only as evidence in the log.
    assert len(spans) == 2, "both offloads should have reported (%r)" % (spans,)
    assert el < 5.0, "executor offloads blocked the loop (%.3fs)" % el


# --------------------------------------------------------------------------
# _driver must coro.send(None): a custom awaitable with no .send
# --------------------------------------------------------------------------
def test_custom_awaitable_without_send_does_not_break():
    class BareIterAwaitable:
        # __await__ returns an iterator that has __next__ but NO send(); a driver
        # that injects a non-None resume value would hit the .send() branch and
        # raise "object has no attribute 'send'".
        def __await__(self):
            class It:
                def __init__(s): s.n = 0
                def __iter__(s): return s
                def __next__(s):
                    s.n += 1
                    if s.n > 3:
                        raise StopIteration("done")
                    return None            # bare yield -> reschedule
            return It()
    async def body():
        return await BareIterAwaitable()
    with hang_guard(20, "custom awaitable no-send"):
        try:
            out = aio.run(body())
        except AttributeError as e:
            pytest.fail("driver injected a resume value into a send-less "
                        "awaitable: %s" % e)
    assert out == "done"


# --------------------------------------------------------------------------
# stress: many concurrent echo connections
# --------------------------------------------------------------------------
# TODO(stackweave): 50 concurrent aio-bridge echo connections intermittently hang
# under a small shared CI runner's contention (the hang_guard(40) fires), same
# load-stress class as swarm_aio_bridge::test_many_concurrent_transport_echo_
# connections.  Passes on a quiet dev box (5/5 in a droplet sweep); it's the
def test_many_concurrent_echo_connections():
    N = 50
    async def body():
        loop = asyncio.get_event_loop()
        class Echo(asyncio.Protocol):
            def connection_made(self, tr): self.tr = tr
            def data_received(self, data):
                self.tr.write(data)
        server = await loop.create_server(Echo, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async def one(i):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            payload = ("msg%d" % i).encode()
            w.write(payload)
            await w.drain()
            data = await r.readexactly(len(payload))
            w.close()
            return data == payload

        results = await asyncio.gather(*[one(i) for i in range(N)])
        server.close()
        await server.wait_closed()
        return results
    with hang_guard(40, "many echo connections"):
        results = aio.run(body())
    assert all(results), "%d/%d echo roundtrips failed" % (results.count(False), N)


def test_stray_wake_credit_does_not_strand_an_await():
    """A wake delivered to a task's fiber while it is NOT park_safe-parked must
    not break that task's NEXT await.

    runloom_sched_wake_safe credits g->wake_pending UNCONDITIONALLY, before its
    parked_safe CAS; only park_safe consumes a credit.  So a wake aimed at a
    RUNNING fiber leaves a credit behind, and the next park_self() returns
    instantly without parking.  The driver used to treat that as "the future
    completed" and call yielded.exception() on a still-PENDING future ->
    InvalidStateError, which its `except asyncio.CancelledError` did not catch:
    the driver fiber died, the coroutine was stranded mid-await with nothing to
    resume it, and the future's later completion found a dead g.

    This is the deterministic form of the load-dependent
    test_swarm_aio_bridge echo hang: there the stray credit came from a real
    race, here we inject it directly.  Pre-fix this hangs; the driver now
    re-parks until the future is genuinely done (or a cancel is pending).
    """
    async def body():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()

        # Deliver a wake to our OWN fiber while it is running.  wake_safe bumps
        # wake_pending; its parked_safe CAS fails (we are not parked), so the
        # credit survives into our next park.
        task._self_g.wake()

        fut = loop.create_future()
        loop.call_later(0.05, fut.set_result, "ok")
        return await fut

    with hang_guard(20, "stray wake credit"):
        assert aio.run(body()) == "ok"


def test_cancel_while_running_interrupts_the_next_park():
    """A cancel that lands while the task is RUNNING must interrupt the park
    that follows it, not wait for the awaited future.

    cancel() with _pgfutwaiter None (running, or in a C park) sets the one-shot
    _pgmustcancel and falls through to _self_g.wake().  The driver checks
    _pgmustcancel at the TOP of its loop -- before the next coro.send, NOT
    before the park that follows -- so without that wake() the cancel is not
    observed until the awaited future completes.  Here it never does, so a
    suppressed wake() hangs forever (measured: rc=124 under a 20s timeout).

    The wake() works by crediting g->wake_pending, which the driver's next
    park_safe consumes as an immediate return.  That credit deliberately
    outlives the wake -- guarding it here so the accounting cleanup around
    runloom_blockpool.c / sched_wake_credit.c does not "fix" it away.
    """
    async def body():
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        # Cancel ourselves while running: _pgfutwaiter is None and we are not
        # netpoll-parked, so cancel() takes the wake() fallback.
        task.cancel()
        never = loop.create_future()      # nothing will ever complete this
        await never
        return "NOT-CANCELLED"

    with hang_guard(20, "cancel while running"):
        with pytest.raises(asyncio.CancelledError):
            aio.run(body())


def test_print_tasks_names_a_waiting_task():
    """The task layer is invisible to dump_fibers: a WAITING task has no live
    fiber (its driver returned into a future) and asyncio.all_tasks() reports
    `<no frames>` because they live in g->snap.  print_tasks must say where the
    coroutine is suspended and what it is waiting on -- the one line that
    identifies a strand above the scheduler."""
    import io
    from stackweave.aio.tasks import print_tasks
    out = io.StringIO()

    async def body():
        loop = asyncio.get_running_loop()
        never = loop.create_future()

        def probe():
            print_tasks(file=out)     # this task is parked on `never` right now
            never.set_result(None)    # release it so the test terminates
        loop.call_later(0.05, probe)
        await never

    with hang_guard(20, "print_tasks"):
        aio.run(body())

    text = out.getvalue()
    assert "aio task dump" in text, text
    # names the suspension point ...
    assert "test_adv_aio.py" in text and ":body" in text, text
    # ... and what it is waiting on, which is the half dump_fibers cannot show
    assert "done=False" in text, text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

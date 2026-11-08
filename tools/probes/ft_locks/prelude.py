import os, sys, threading, time
import stackweave, stackweave_c

def _watchdog(secs):
    def fire():
        print("WATCHDOG TIMEOUT after %ss" % secs, flush=True)
        import faulthandler; faulthandler.dump_traceback(all_threads=True)
        os._exit(3)
    t = threading.Timer(secs, fire); t.daemon = True; t.start()

def foreign_send(ch, value):
    """Send from a plain OS thread.  A non-fiber thread cannot block, so an
    unbuffered send raises RuntimeError while no receiver is parked yet (a
    slow box reaches the send before the fiber reaches its recv); retry
    until the hand-off happens."""
    while True:
        try:
            ch.send(value)
            return
        except RuntimeError:
            time.sleep(0.0005)

def _foreign_wake(ch, i):
    t = threading.Thread(target=lambda: (time.sleep(0.001), foreign_send(ch, i)),
                         daemon=True)
    t.start()
    ch.recv()
    t.join()

def force_migrate(rounds=40, pin_fallback=True):
    """Move the calling fiber to another hub's OS thread; True once it has.

    First it parks on a channel until a FOREIGN OS thread wakes it, up to
    `rounds` times: a hub-thread waker would push it onto its own deque
    (local wake) and it would usually resume right there, while a foreign
    wake goes through the global run-queue to whichever hub pulls it.  On an
    idle box the hub it parked on often pulls it every time, so then it pins
    the next resume to another hub, which routes one more foreign wake there,
    and unpins.  pin_fallback=False measures natural migration alone."""
    ch = stackweave.Chan(0)
    for i in range(rounds):
        before = threading.get_ident()
        _foreign_wake(ch, i)
        if threading.get_ident() != before:
            return True
    if not pin_fallback:
        return False
    g = stackweave_c.current_g()
    before = threading.get_ident()
    g.pin((stackweave_c.mn_current_hub() + 1) % stackweave_c.mn_hub_count())
    try:
        _foreign_wake(ch, rounds)
    finally:
        g.pin(None)
    return threading.get_ident() != before

def park_n_times(n):
    """Foreign-thread wakes only; returns the set of OS thread ids we ran on."""
    ch = stackweave.Chan(0)
    tids = {threading.get_ident()}
    for i in range(n):
        _foreign_wake(ch, i)
        tids.add(threading.get_ident())
    return tids

def require_migration(moved):
    """Migration is always on, so a scenario that never saw one is a failure
    (NOMIG), never a pass or a skip: its invariant went unchecked."""
    if not moved:
        print("NOMIG: the scenario saw no cross-hub migration", flush=True)
        os._exit(4)

def held_by_this_fiber(module):
    """Whether the calling fiber holds `module`'s import lock as itself: the
    lock is stackweave's (stackweave/_importlock.py) and names this fiber,
    not the OS thread it is on."""
    import importlib._bootstrap as bootstrap
    lock = bootstrap._module_locks[module]()
    return (type(lock).__name__ == "_FiberModuleLock"
            and lock.owner == stackweave_c.current_g())
__all__ = [n for n in list(globals()) if not n.startswith('__')]

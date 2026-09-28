"""``@stackweave.hot`` -- mark a hot handler so it scales cleanly across all cores.

The problem it quietly fixes (you don't need the details): when a handler is a
CLOSURE -- it *captures* something, e.g. ``handler = make_app(config)`` -- and the
SAME closure runs flat-out on many cores at once, every core hammers the same
captured slots and they start fighting over them, so adding cores stops helping.
``@stackweave.hot`` gives each core its own private copy of those captured slots
(pointing at the same values), so they stop colliding and your scaling returns.

    config = load_config()

    @stackweave.hot
    def handle(conn):
        serve(conn, config)          # `config` is captured -> shared across cores

A plain module-level ``def`` that captures nothing already scales perfectly --
there's nothing shared to fight over -- so ``@stackweave.hot`` is a safe no-op there
(leave it on, it costs nothing).  When it does kick in it costs a little memory:
one copy of the captured slots **per core**, NOT per fiber.

It stays correct: it only splits captures your handler just READS (the usual
config/state case).  If the handler REBINDS a captured name (``nonlocal x; x =
...``), per-core copies could drift, so stackweave leaves it shared instead.

FASTEST PATH FIRST: if a handler is hot enough to want this, *compiling* it (a
Cython ``cdef`` handler) beats it outright -- that removes the interpreter cost
entirely.  ``@stackweave.hot`` is the zero-rewrite option.  Stacking with other
decorators: put ``@stackweave.hot`` CLOSEST to your ``def`` so it sees your real
closure, not another decorator's wrapper.
"""
import dis
import functools
import threading
import types


def _rebinds_capture(code):
    # True iff the function REBINDS one of its captured (free) variables, i.e.
    # ``nonlocal x; x = ...`` -> compiles to STORE_DEREF/DELETE_DEREF on a
    # freevar.  Then per-core copies would diverge, so we must not split them.
    # Mutating a captured OBJECT in place (config.x = ..., d[k] = v) is fine --
    # that's STORE_ATTR/STORE_SUBSCR, and every copy points at the same object.
    free = frozenset(code.co_freevars)
    if not free:
        return False
    # The rebind can also live in a NESTED function that shares the cell via
    # ``nonlocal`` -- its STORE_DEREF sits in a nested code object hanging off
    # co_consts, which dis.get_instructions() does not descend into.  Scan those
    # too, recursively, matching on the captured NAME (name-matching may
    # over-report a same-named cell from a deeper scope, but that only keeps a
    # cell shared -- the safe side of the "leave it shared rather than be subtly
    # wrong" contract).
    return _rebinds_names(code, free)


def _rebinds_names(code, free):
    for ins in dis.get_instructions(code):
        if ins.opname in ("STORE_DEREF", "DELETE_DEREF") and ins.argval in free:
            return True
    for const in code.co_consts:
        if isinstance(const, types.CodeType) and _rebinds_names(const, free):
            return True
    return False


def hot(fn):
    """Mark a hot handler for per-core scaling.  See the module docstring.

    Returns a thin wrapper that, the first time your handler runs on a given
    core, hands that core its own copy of the captured slots and reuses it
    thereafter.  A safe no-op on anything that isn't a closure that only reads
    its captures.
    """
    # Only a plain Python function carries a closure we can split.  Anything else
    # (a builtin, a class, an already-wrapped C callable) passes straight through.
    if not isinstance(fn, types.FunctionType):
        return fn
    # The contention is SHARED CLOSURE CELLS.  No captures -> nothing shared ->
    # it already scales, so @hot is a no-op.  Rebinds a capture -> splitting it
    # would change behaviour, so leave it shared rather than be subtly wrong.
    if not fn.__closure__ or _rebinds_capture(fn.__code__):
        return fn

    copies = {}              # core (OS-thread id) -> that core's private copy
    lock = threading.Lock()  # guards first-touch insertion only

    def _copy_for(core):
        c = copies.get(core)
        if c is not None:
            return c
        with lock:
            c = copies.get(core)
            if c is None:
                # Fresh cells holding the SAME values -> identical behaviour, but
                # this core stops sharing the cells with the others.  The code
                # object is shared on purpose: it isn't the contended part.
                cells = tuple(types.CellType(cell.cell_contents)
                              for cell in fn.__closure__)
                c = types.FunctionType(fn.__code__, fn.__globals__, fn.__name__,
                                       fn.__defaults__, cells)
                c.__kwdefaults__ = fn.__kwdefaults__
                c.__dict__.update(fn.__dict__)
                copies[core] = c
        return c

    @functools.wraps(fn)
    def runner(*args, **kwargs):
        # One hub == one OS thread, so the thread id is the core key; the copy
        # count is bounded by hub count, never by fiber count.  (A fiber that
        # work-steals to another hub mid-run keeps its origin copy for that call;
        # the sharing factor still drops from "all fibers" to "a few per core".)
        return _copy_for(threading.get_ident())(*args, **kwargs)

    runner.__runloom_hot__ = True
    runner._runloom_copies = copies          # introspection / tests
    return runner

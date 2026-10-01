"""Type stubs for stackweave."""
from collections.abc import Callable
from typing import Any, TypeVar

# Core primitives re-exported from the C extension so `import stackweave` suffices.
from stackweave_c import (
    G as G,
    Chan as Chan,
    select as select,
    mn_init as mn_init,
    mn_fiber as mn_fiber,
    mn_run as mn_run,
    mn_fini as mn_fini,
    mn_hub_count as mn_hub_count,
    netpoll_backend as netpoll_backend,
)

_T = TypeVar("_T")

__version__: str

def blocking(fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Offload a blocking/CPU-bound call to a worker pool; park until done."""
    ...

class Goroutine:
    """Handle returned by fiber() on the single-thread scheduler.  Read-only
    views of the underlying stackweave_c.G; no join or cancel methods
    (cancel cooperatively via stackweave.context)."""

    name: str
    @property
    def done(self) -> bool: ...
    @property
    def result(self) -> Any: ...
    @property
    def exception(self) -> BaseException | None: ...
    @property
    def coro(self) -> Goroutine: ...  # compat alias: returns self

def fiber(
    callable_: Callable[..., _T],
    /,
    *args: Any,
    **kwargs: Any,
) -> Goroutine | None:
    """Spawn a goroutine.  Same semantics as `go fn(a, b)` in Go:
    schedules fn(*args, **kwargs) to run cooperatively, returns immediately.

    Returns a Goroutine handle on the single-thread scheduler (run(1, ...));
    inside an M:N run (run(n > 1, ...)) it spawns onto a hub via mn_fiber,
    which returns no handle, so it returns None."""
    ...

def yield_() -> None:
    """Cooperative yield.  Equivalent to runtime.Gosched()."""
    ...

def sleep(seconds: float) -> None:
    """Sleep without blocking the OS thread.  Other goroutines run."""
    ...

def run(n: int, main_fn: Callable[[], Any] | None = ...) -> int:
    """THE entry point: run the scheduler on n OS-thread hubs until idle.

        run(1, main)   single-thread (M:1).
        run(n, main)   M:N across n hubs, GIL off -> real multi-core
                       parallelism.  Requires a free-threaded build (3.14t+,
                       PYTHON_GIL=0); n > 1 with the GIL on raises.
        run(n)         main_fn omitted -> drain already-fiber()'d goroutines.

    n is required and explicit: M:N is a different correctness model (Python
    runs in parallel, so shared state can race), opted into by typing the
    number.  main_fn, when given, is the root goroutine and may fiber() more.
    Collapses the raw mn_init/mn_fiber/mn_run/mn_fini envelope.  Returns the
    number of goroutines completed."""
    ...

def current() -> G | None:
    """Return the currently-running goroutine's stackweave_c.G handle, or
    None when called from outside any goroutine."""
    ...

def backend() -> str:
    """Coroutine backend name: 'fcontext-asm' | 'ucontext'."""
    ...

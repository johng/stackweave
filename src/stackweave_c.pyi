"""Type stubs for the stackweave_c C extension."""
from collections.abc import Callable
from typing import Any, Literal, overload

# ---- Coroutine handle (raw, no scheduler) -----------------------------

class Coro:
    """A raw stackful coroutine.  Most users want fiber()/run() instead.

    Resumed from an M:N fiber, the body runs on that fiber's stack but is not
    itself a fiber: the hub is hidden from it, so current_g() is None,
    in_fiber() is False and mn_current_hub() is None there, stackweave.sleep()
    falls back to time.sleep(), and sysmon does not preempt it.  Its spawns
    still go to the hubs (mn_spawns_to_hubs())."""
    def __init__(self, callable: Callable[..., Any], stack_size: int = ...) -> None: ...
    @property
    def done(self) -> bool: ...
    @property
    def result(self) -> Any: ...
    def resume(self) -> Any: ...

# ---- Goroutine handle (scheduler-aware) -------------------------------

class G:
    """Goroutine handle returned by fiber() and current_g()."""
    @property
    def done(self) -> bool: ...
    @property
    def result(self) -> Any: ...
    @property
    def exception(self) -> BaseException | None: ...
    def wake(self) -> None: ...
    def stack(self) -> dict[str, Any]: ...
    def pin(self, hub: int | None) -> None: ...
    def cancel_wait_fd(self) -> bool: ...

# ---- Channel ---------------------------------------------------------

class Chan:
    """Go-style channel.  Buffered if capacity > 0, unbuffered otherwise."""
    def __init__(self, capacity: int = ...) -> None: ...
    @property
    def capacity(self) -> int: ...
    @property
    def closed(self) -> bool: ...
    def send(self, value: Any, /) -> None: ...
    def recv(self) -> tuple[Any, bool]: ...
    def try_send(self, value: Any, /) -> bool: ...
    def try_recv(self) -> tuple[Any, bool] | None: ...
    def close(self) -> None: ...
    def __iter__(self) -> Chan: ...
    def __next__(self) -> Any: ...
    def __len__(self) -> int: ...

# ---- Single-thread scheduler -----------------------------------------

def fiber(fn: Callable[[], Any], stack_size: int = ...) -> G:
    """Spawn a goroutine on the single-thread C scheduler.  Returns handle.
    stack_size > 0 overrides the default C stack for this one fiber.  Called
    from an M:N fiber, the scheduler is that fiber's own, which only its own
    run() drives."""
    ...

def fiber_noyield(callable_: Callable[[], Any], /) -> G:
    """Spawn a goroutine the caller PROMISES runs to completion without
    yielding.  Skips per-g snap/load dance.  150-400 ns/g faster.
    Undefined behaviour if the callable yields."""
    ...

def run() -> int:
    """Drive the scheduler until all goroutines complete.  Returns count.

    Called from an M:N fiber it drives that fiber's own scheduler and holds
    the hub until it returns, so the hub's other fibers wait meanwhile."""
    ...

def yield_() -> None:
    """Yield from inside a raw Coro (no-op outside one)."""
    ...

def sched_yield_classic() -> None:
    """Yield the current goroutine.  Slower form for benchmarking."""
    ...

def sched_sleep(seconds: float, /) -> None:
    """Sleep the current goroutine N seconds.  Scheduler-aware."""
    ...

# ---- Backend introspection -------------------------------------------

def backend() -> Literal["fcontext-asm", "ucontext"]:
    """Coroutine stack-switch backend."""
    ...

def netpoll_backend() -> Literal["epoll", "kqueue", "select"]:
    """Active netpoll backend selected at first init."""
    ...

# ---- netpoll -----------------------------------------------------------

def wait_fd(fd: int, events: int, timeout_ms: int = ..., /) -> int:
    """Park the current goroutine until fd is ready.  events bitmask:
    1=read, 2=write.  Returns the readiness mask."""
    ...

def select(
    cases: list[tuple[str, Chan] | tuple[str, Chan, Any]],
    default: bool = ...,
) -> tuple[int, Any] | Literal[-1]:
    """Wait on multiple channels.  Each case is ('recv', ch) or
    ('send', ch, value).  Returns (index, (value, ok)) for recv or
    (index, None) for send.  With default=True returns -1 if no case
    is immediately ready."""
    ...

# ---- C-level socket fast path ----------------------------------------

def tcp_recv(fd: int, buffer: bytearray | memoryview, n: int,
             flags: int = ..., /) -> int:
    """recv into buffer; returns bytes received.  Cooperative blocking."""
    ...

def tcp_send(fd: int, data: bytes | bytearray | memoryview,
             flags: int = ..., /) -> int:
    """sendall; returns bytes_sent.  Cooperative blocking."""
    ...

# ---- Per-thread + warmup ---------------------------------------------

def thread_init() -> None:
    """Idempotent per-thread setup."""
    ...

def thread_fini() -> None:
    """Per-thread teardown."""
    ...

def warmup(n: int, stack_size: int = ..., /) -> int:
    """Pre-allocate n stacks of stack_size bytes for the per-thread
    stack pool.  Returns actual count.  Eliminates first-spawn mmap
    latency on server workloads."""
    ...

# ---- M:N scheduler -----------------------------------------------------

def mn_init(n: int = ...) -> int:
    """Start N hub threads (default: nproc).  Returns count."""
    ...

def mn_fiber(fn: Callable[[], Any], stack_size: int = 0,
             hub: int = -1) -> None:
    """Spawn on a round-robin hub.  Returns no handle.

    stack_size>0 overrides the default C-stack (bytes) for a
    goroutine that runs a deep, non-yielding C burst (cold imports,
    terminfo/OpenSSL init) that the resume-boundary copy-grow can't rescue.

    hub=N spawns on hub N and keeps it there (not stealable) -- a
    determinism knob for tests, not affinity.
    """
    ...

def mn_run() -> int:
    """Wait for all M:N goroutines to complete.  Returns total."""
    ...

def mn_fini() -> None:
    """Tear down the hub pool."""
    ...

def mn_hub_count() -> int:
    """Number of M:N hubs currently running (0 outside an M:N run)."""
    ...

def mn_spawns_to_hubs() -> bool:
    """Whether a spawn from here goes to the M:N hubs: an M:N runtime is up
    and the caller is not inside a run(1) nested on an M:N fiber, whose
    spawns stay in it."""
    ...

# ---- Preemption ------------------------------------------------------

def preempt_init(quantum_us: int = ..., /) -> None:
    """Start the time-sliced preemption timer."""
    ...

def preempt_fini() -> None:
    """Stop the preemption timer if running."""
    ...

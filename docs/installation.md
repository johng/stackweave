# Installation

stackweave is a C extension that needs a compiler at build time.
`pip install stackweave` builds it from source: there are no prebuilt
wheels (see [below](#prebuilt-wheels)).

## Requirements

- **Free-threaded (`--disable-gil`) CPython 3.14 or newer.**  `setup.py`
  refuses GIL builds and older versions: the fiber stack floor, the per-fiber
  stack-overflow check and the parked-frame GC anchor all depend on it, and the
  C sources have no other code paths.
- **Built with stackweave's migration patches** (alloc-home + exec-home) --
  see [src/patches/](https://github.com/johng/stackweave/blob/main/src/patches/README.md)
  (`tools/ci/build_patched_cpython.sh 314` builds one).  M:N runs are not
  sound without them, so `pip install` refuses a stock interpreter.
- A C compiler.  Anything reasonably modern works: GCC 4.7+ or Clang
  3.5+.

## Editable install

Use the patched interpreter's pip:

```bash
git clone https://github.com/johng/stackweave
cd stackweave
/path/to/patched/bin/python3.14 -m pip install -e .
```

On a stock free-threaded 3.14t, pip refuses the install.  To build and run
the test suite there anyway, build in place and run with `PYTHONPATH=src`
(or set `STACKWEAVE_ALLOW_STOCK_CPYTHON=1` to let pip through):

```bash
python3.14t setup.py build_ext --inplace
```

## No compiler? Bootstrap helpers

The `scripts/` directory contains a detect-and-install wrapper that
fetches a compiler before invoking pip:

```bash
./scripts/install.sh                # detects distro, installs gcc/clang
./scripts/install.sh --editable     # passes -e through to pip
```

The orchestrator probes for `gcc`/`clang` on PATH and invokes
`bootstrap_compiler.sh` if missing.  The bootstrap installer knows:
`apt-get`, `dnf/yum`, `pacman`, `zypper`, `apk`, `xbps`,
FreeBSD/OpenBSD/NetBSD `pkg`, `pkgin`, Haiku `pkgman`, and macOS
Command Line Tools.

## Build-time environment knobs

| Variable | Effect |
| --- | --- |
| `STACKWEAVE_BACKEND=ucontext` | Force the ucontext stack-swap backend even on x86_64/aarch64. |
| `STACKWEAVE_NO_ASM=1` | Drop the `.S` source from the build (same effect as above). |
| `STACKWEAVE_DEBUG=1` | `-O0 -g`. |
| `STACKWEAVE_EXTRA_CFLAGS` | Appended to the compile command line. |
| `STACKWEAVE_EXTRA_LDFLAGS` | Appended to the link command line. |
| `CC` | Usual setuptools override. |

## Interpreter build: the tier-2 JIT is off, TLBC is on

Two pieces of CPython's optimising machinery interact with the fiber
scheduler, and stackweave's position on them is different for each. The
difference is worth stating plainly, because one is a decision and the
other is only a default.

### The tier-2 JIT: off, and never yet evaluated

Every interpreter stackweave is developed, tested and deployed against has
the tier-2 JIT **off**:

| build | configure | JIT |
| --- | --- | --- |
| dev 3.14.4t | `--disable-gil` | off |
| production (soupchan/ovh1) | `--disable-gil` | off |

It is off because CPython's JIT is opt-in at build time
(`--enable-experimental-jit`) and **nobody has ever turned it on** — not
because it was evaluated and rejected. There is no code in this tree that
tests for it, gates on it, or works around it. Treat "stackweave with the JIT"
as an untried configuration rather than a supported-but-disabled one.

What actually depends on this today: `PyThreadState.current_executor`
(added in 3.14) is always `NULL` in a non-JIT build, so stackweave never
observes it and `tools/verify/tstate_manifest.json` classifies it
`OWNER_ONLY` — correct by inspection, but **unverified against a build
where the field is ever non-NULL**.

So before enabling the JIT:

1. Re-derive `current_executor`'s disposition against a live tier-2 build.
   If a fiber can park while an executor is in flight, `OWNER_ONLY` may be
   the wrong call, and getting it wrong resurrects an executor into a
   dispatch that never entered it. The manifest note says the same thing.
2. Run a soak. `tools/soak` records `cpu_pct` and the slope oracle will
   flag a runtime that starts burning CPU it did not used to burn —
   which is the shape most scheduler/interpreter interaction bugs have.

### TLBC: on, deliberately, with an interlock

Thread-local bytecode is the opposite case — a considered stance backed by
a crash. On free-threaded 3.14+ TLBC turns on the specializing interpreter,
and without it pure-Python loops *anti-scale* (~13x slower at 8 threads,
measured). But "TLBC on + no visibility into parked fiber frames" was a
reproducible SIGSEGV (`src/runloom_c/module_gcframes.c.inc`, the big_100
p565/p524 crash).

The resolution is an interlock rather than a blanket disable, in
`stackweave/runtime.py` (`_tlbc_reexec_if_needed`): the GC-frames anchor makes
parked fiber frames visible to the collector, and TLBC stays **on** whenever
that anchor is active — the default. Only when the anchor is
unavailable (an anchor init failure, or a build where the fix compiled out) does stackweave re-exec with `PYTHON_TLBC=0`, which
keeps the crashy combination unreachable. Opt out entirely with
`PYTHON_TLBC=0` / `-X tlbc=0`.

## Verifying the install

```python
import stackweave, stackweave_c
print("backend:", stackweave.backend())            # e.g. fcontext-asm
print("netpoll:", stackweave.netpoll_backend())    # e.g. epoll
print("stack default:", stackweave_c.get_stack_size(), "bytes")

def hello():
    print("hello from a fiber!")
stackweave.fiber(hello)
stackweave.run(1)
```

If `backend()` returns `"fcontext-asm"`, you're on the fast path (~80
ns per context switch).  `"ucontext"` is the POSIX fallback.

## Platform support

| OS / arch | stack switch | netpoll | tested |
| --- | --- | --- | --- |
| Linux x86_64 (Debian 13, Fedora 39) | fcontext-asm | epoll | yes |
| Linux aarch64 | fcontext-asm | epoll | qemu-aarch64 |
| macOS Big Sur x86_64 | fcontext-asm | kqueue | yes |
| macOS arm64 (Apple Silicon) | fcontext-asm | kqueue | yes (CI: macos-14) |
| FreeBSD 14.3 / GhostBSD x86_64 | fcontext-asm | kqueue | on 3.12 only -- not yet re-validated on 3.14t |
| OpenBSD / NetBSD / DragonFly | fcontext-asm | kqueue | code review |
| Solaris / illumos | ucontext | select | code review |
| Android (Termux) | fcontext-asm | epoll | code review |

Windows is not supported.

## Prebuilt wheels

`pyproject.toml` still carries a `[tool.cibuildwheel]` matrix covering
free-threaded CPython 3.14+ (`cp314t`) on:

- Linux x86_64 + aarch64 (manylinux\_2\_28)
- macOS universal2 (arm64 + x86_64)

but no wheels are published.  The patched and stock interpreters share the
`cp314t` wheel tag, so pip could not keep a prebuilt wheel off stock
CPython; and `bdist_wheel` -- which cibuildwheel drives -- is behind the
same install gate as `pip install`, so it refuses a stock build interpreter
unless `STACKWEAVE_ALLOW_STOCK_CPYTHON=1` is set.

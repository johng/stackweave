# Installation

stackweave is a C extension that needs a compiler at build time.  Once
prebuilt wheels are uploaded (see roadmap), the simplest install path
will be plain `pip install stackweave`; until then build from source.

## Requirements

- **Python 3.11 or newer.**  The per-fiber `PyThreadState`
  snapshot uses 3.11+ tstate fields (`cframe`, `datastack_chunk`,
  `exc_state`).  Pre-3.11 used a different frame model that stackweave
  doesn't cover.
- A C compiler.  Anything reasonably modern works: GCC 4.7+, Clang
  3.5+, MSVC 19.20+ (VS 2019 16.0+), MinGW-w64.
- Free-threaded 3.14t (and 3.13t) are fully supported and add the M:N
  work-stealing scheduler + time-sliced preemption features.

## Editable install

```bash
git clone https://github.com/johng/stackweave
cd stackweave
pip install -e .
```

On free-threaded 3.14t:

```bash
~/.pyenv/versions/3.14.4t/bin/python3.14 -m pip install -e .
```

## No compiler? Bootstrap helpers

The `scripts/` directory contains detect-and-install wrappers that
fetch a compiler before invoking pip:

=== "POSIX (Linux/macOS/BSD)"

    ```bash
    ./scripts/install.sh                # detects distro, installs gcc/clang
    ./scripts/install.sh --editable     # passes -e through to pip
    ```

=== "Windows"

    ```bat
    scripts\install.bat                 :: auto-detects MSVC, falls back to MinGW
    scripts\install.bat --editable
    ```

The orchestrator scripts probe for `gcc`/`clang`/`cl` on PATH and
invoke `bootstrap_compiler.{sh,ps1,bat}` if missing.  The bootstrap
installer knows: `apt-get`, `dnf/yum`, `pacman`, `zypper`, `apk`,
`xbps`, FreeBSD/OpenBSD/NetBSD `pkg`, `pkgin`, Haiku `pkgman`, macOS
Command Line Tools, MSVC Build Tools (via `vs_BuildTools.exe`), and
MinGW-w64 (via the WinLibs zip).

## Build-time environment knobs

| Variable | Effect |
| --- | --- |
| `STACKWEAVE_BACKEND=ucontext` | Force the ucontext stack-swap backend even on x86_64/aarch64. |
| `STACKWEAVE_NO_ASM=1` | Drop the `.S` source from the build (same effect as above). |
| `STACKWEAVE_NO_IOCP=1` | Omit the Windows IOCP-AFD backend (falls back to WSAPoll/select). |
| `STACKWEAVE_DEBUG=1` | `-O0 -g` (POSIX) or `/Od /Zi` (MSVC). |
| `STACKWEAVE_EXTRA_CFLAGS` | Appended to the compile command line. |
| `STACKWEAVE_EXTRA_LDFLAGS` | Appended to the link command line. |
| `CC` | Usual setuptools override; controls compiler selection on Windows too. |

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
| dev 3.13.13t | `--disable-gil` | off |
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
that anchor is active — the default on FT 3.14+. Only when the anchor is
unavailable (e.g. 3.13t) does stackweave re-exec with `PYTHON_TLBC=0`, which
keeps the crashy combination unreachable. Opt out entirely with
`PYTHON_TLBC=0` / `-X tlbc=0`; force it on regardless with `STACKWEAVE_TLBC=1`
(dev/debug only).

## Verifying the install

```python
import stackweave
print("backend:", stackweave.backend())            # e.g. fcontext-asm
print("netpoll:", stackweave.netpoll_backend())    # e.g. epoll
print("stack default:", stackweave.get_stack_size(), "bytes")

def hello():
    print("hello from a fiber!")
stackweave.fiber(hello)
stackweave.run(1)
```

If `backend()` returns `"fcontext-asm"`, you're on the fast path (~80
ns per context switch).  `"fibers"` means Windows Fibers (slightly
slower).  `"ucontext"` is the POSIX fallback.

## Platform support

| OS / arch | stack switch | netpoll | tested |
| --- | --- | --- | --- |
| Linux x86_64 (Debian 13, Fedora 39) | fcontext-asm | epoll | yes |
| Linux aarch64 | fcontext-asm | epoll | qemu-aarch64 |
| macOS Big Sur x86_64 | fcontext-asm | kqueue | yes |
| macOS arm64 (Apple Silicon) | fcontext-asm | kqueue | code review |
| FreeBSD 14.3 / GhostBSD x86_64 | fcontext-asm | kqueue | yes |
| OpenBSD / NetBSD / DragonFly | fcontext-asm | kqueue | code review |
| Solaris / illumos | ucontext | select | code review |
| Android (Termux) | fcontext-asm | epoll | code review |
| Windows 11 / 10 / Server 2022 | Fibers | WSAPoll | yes |
| Windows 8.1 (MinGW-w64) | Fibers | select | yes |

Windows backend selection happens at runtime: `WSAPoll` is probed via
`GetProcAddress` at first netpoll init, falling back to `select()` on
hosts where it's missing (XP/Server 2003).  One binary works across
Windows Vista through Windows 11.

## Prebuilt wheels

`pyproject.toml` ships a `[tool.cibuildwheel]` matrix covering CPython
3.11–3.14 on:

- Linux x86_64 + aarch64 (manylinux\_2\_28)
- macOS universal2 (arm64 + x86_64)
- Windows AMD64

Run `cibuildwheel --output-dir wheels` from a CI runner (or locally
with Docker) to populate `wheels/` for upload to PyPI.

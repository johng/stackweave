# stackweave tests

## Running

    PYTHON_GIL=0 python tests/run_isolated.py                  # every tests/test_*.py
    PYTHON_GIL=0 python tests/run_isolated.py test_chan.py -k close
    PYTHON_GIL=0 PYTHONPATH=src python -m pytest tests/test_chan.py

`run_isolated.py` runs each file in its own interpreter. The in-process
`pytest tests/` flakes on state that leaks across files. The suite only runs
where cross-hub migration, which is always on, is sound:

- a free-threaded interpreter with the GIL off;
- built with both `src/patches/` halves;
- an extension built with them.

`run_isolated.py` and `conftest.py` stop with the reason anywhere else. Nothing
skips for want of free threading or M:N, so a stock interpreter fails loudly
instead of passing on a fraction of the suite. CLAUDE.md, "Build & test", has
the build recipe.

## Layout

Top-level `test_*.py` files are the suite. File-name prefixes say where a file
came from:

| prefix | what it is |
|---|---|
| `test_adv_*` | adversarial QA: drives a subsystem at its failure modes (lost wakes, teardown hangs, foreign threads) |
| `test_swarm_*` | larger adversarial sweeps, one file per subsystem |
| `test_cov_*`, `test_cov95_*`, `test_cov100*_*` | coverage gap-fills: each names the C lines it reaches |
| `test_*_compat`, `test_stdlib_*_monkey` | stdlib behaviour under `monkey.patch()`; `stdlib_*` runs CPython's own test bodies |
| `test_*_faultinject`, `test_*_faults_*` | syscall fault injection (strace, LD_PRELOAD) |
| `test_kqueue_*`, `test_iouring_*`, `test_netpoll_*` | one netpoll backend each |
| `test_mn_sim_*`, `test_simfd_*`, `test_chess_*` | the seeded M:N scheduler, currently disabled (`SEEDED_MN_TODO`) |
| `test_lifefuzz_*`, `test_linz_*`, `test_dst_*`, `test_snowboard_*` | harnesses from `tools/` |
| anything else | a subsystem or a regression, named for it |

Helpers:

- **`conftest.py`** checks after every test: the scheduler's structural self-check, no leaked netpoll parker, no swallowed error. It also holds the migration gate.
- **`known_gaps.py`** has the xfail markers.
- **`adv_util.py`** has the shared helpers: `run_python()` / `child_env()` for child interpreters, `hang_guard()`, `assert_faster_than()`, `raw_thread()`, and `ensure_fd_budget()`.

Subdirectories are not collected by `run_isolated.py` unless named with `--suite`:

| directory | what it is |
|---|---|
| `aio/` | CPython's `test_asyncio` bodies on `StackweaveEventLoop` (`run_isolated.py --suite aio`) |
| `net/` | real-network tests, opt-in with `STACKWEAVE_NET_TESTS=1` |
| `big_100/` | the long-running stress and soak corpus; the conservation programs feed the metamorphic phase of `scripts/check_all.sh` |
| `tests_c/` | C-level tests and benches (`make -C tests/tests_c`) |
| `tests_stdlib/` | CPython's stdlib suite run under M:N |
| `bughunt_repros/`, `regressions/` | standalone reproducer scripts, run by hand |
| `demo/`, `experiments/`, `synthetic/` | demos, measurements and generated workloads |

## Conventions

- **Known gaps are xfail, never skip.** Mark one with a `known_gaps.py` helper: `KNOWN_GAP`, `MIGRATION_GAP`, `INTERMITTENT`, `SEEDED_MN_TODO` or `GON_BULK_GAP`. Never use a bare `pytest.mark.xfail`; conftest rejects it. Each one asserts the correct behaviour and is strict, so a closed gap XPASSes and fails the run until the marker comes off. `pytest -m known_gap` lists them.
- **A skip means the platform lacks something** (Linux-only, epoll, io_uring, strace, x86-64, a tool that isn't built, an optional package) or the test is opt-in by env var. It never stands in for a bug.
- **A child interpreter goes through `run_python()`** (or `child_env()` when the test needs Popen, bytes or another cwd). The child gets the GIL off and `src/` on PYTHONPATH. A hang fails the test with the child's output; it is never a skip.
- **A test that crashes or hangs on purpose** runs in a child interpreter, so the crash or hang stays contained.
- **File header:** the module docstring, then imports grouped stdlib / third-party / stackweave / test helpers. Each file ends with `if __name__ == "__main__": sys.exit(pytest.main([__file__] + sys.argv[1:]))`, so it can be run directly.

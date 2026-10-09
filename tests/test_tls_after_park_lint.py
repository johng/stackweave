"""tools/ci/check_tls_after_park.py: no thread-local address survives a park.

A fiber that parks can resume on another hub thread, but clang computes a
thread-local's address once per function and reuses it after calls.  A use
after a call that can park then touches the origin thread's copy (TSan finding
A2 in docs/dev/TSAN.md: runloom_chan_select advanced the origin hub's select
PRNG).  The lint finds that reuse in the built extension's machine code.

The fixtures give it teeth: the same functions compiled with and without the
reuse must flip its verdict, for a TLS address and for the thread pointer
(`mrs TPIDRRO_EL0`, which exec-home reads through volatile asm for the same
reason).  arm64 macOS only, like the lint.
"""
import importlib.util
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LINT = ROOT / "tools" / "ci" / "check_tls_after_park.py"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or platform.machine() != "arm64",
    reason="the lint reads arm64 Mach-O code")

# The names the lint keys on: the park primitive and an anchor TLS reader.
FIXTURE = r"""
static __thread unsigned long counter;
__thread void *current;

__attribute__((noinline)) void runloom_coro_yield(void)
{
    __asm__ volatile("" ::: "memory");
}

void *runloom_mn_tls_current_g(void) { return current; }

#if TID_ASM_VOLATILE
#  define TID_ASM __asm__ __volatile__
#else
#  define TID_ASM __asm__
#endif
static inline unsigned long tid(void)
{
    unsigned long t;
    TID_ASM("mrs %0, tpidrro_el0" : "=r"(t));
    return t;
}

#if REUSE
/* One TLS address computation serves both increments. */
unsigned long bump(void) { counter++; runloom_coro_yield(); return ++counter; }
#else
static __attribute__((noinline)) unsigned long next(void) { return ++counter; }
unsigned long bump(void) { next(); runloom_coro_yield(); return next(); }
#endif

/* Without volatile, the second thread-pointer read is the first one's value. */
int owned_after_park(unsigned long *owner)
{
    owner[0] = tid();
    runloom_coro_yield();
    return owner[1] == tid();
}
"""


# The address handed to an out-of-line helper instead of used inline: the
# helper's load/store base is an argument register, so the caller passing a
# stale address in it is a stale use.  Each CASE compiles on its own, after the
# same prelude.
PRELUDE = r"""
#include <stdint.h>
__thread void *current;
__attribute__((noinline)) void runloom_coro_yield(void)
{
    __asm__ volatile("" ::: "memory");
}
void *runloom_mn_tls_current_g(void) { return current; }
static __thread uint64_t rng = 1;
"""
CASES = {
    # A2 again, behind a noinline PRNG step that takes the state by pointer:
    # the caller resolves &rng once and passes it to both draws.
    "helper_by_pointer": (r"""
static __attribute__((noinline)) uint64_t step(uint64_t *s)
{ uint64_t x = *s; x ^= x << 13; x ^= x >> 7; x ^= x << 17; *s = x; return x; }
uint64_t sel(void) { uint64_t a = step(&rng); runloom_coro_yield(); return a + step(&rng); }
""", True),
    # The same, the second use a tail call.
    "tail_call": (r"""
__attribute__((noinline)) uint64_t use(uint64_t *p) { return ++*p; }
uint64_t f(void) { rng++; runloom_coro_yield(); return use(&rng); }
""", True),
    # The fix: the helper resolves the thread-local itself, on every call.
    "helper_resolves_itself": (r"""
static __attribute__((noinline)) uint64_t step(void)
{ uint64_t x = rng; x ^= x << 13; x ^= x >> 7; x ^= x << 17; rng = x; return x; }
uint64_t sel(void) { uint64_t a = step(); runloom_coro_yield(); return a + step(); }
""", False),
}


def lint(path):
    return subprocess.run([sys.executable, str(LINT), str(path)],
                          capture_output=True, text=True, timeout=300)


def test_built_extension_passes():
    spec = importlib.util.find_spec("stackweave_c")
    assert spec is not None and spec.origin, "stackweave_c is not built"
    r = lint(spec.origin)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("reuse,volatile_tid,flagged", [
    (1, 1, ["[counter]"]),
    (0, 1, []),
    (0, 0, ["[thread pointer]"]),
    (1, 0, ["[counter]", "[thread pointer]"]),
])
def test_fixture_verdicts(tmp_path, reuse, volatile_tid, flagged):
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no C compiler")
    src = tmp_path / "fixture.c"
    src.write_text(FIXTURE)
    so = tmp_path / "fixture.so"
    subprocess.run([cc, "-O2", "-arch", "arm64", "-bundle", "-undefined",
                    "dynamic_lookup", "-DREUSE=%d" % reuse,
                    "-DTID_ASM_VOLATILE=%d" % volatile_tid, "-o", str(so), str(src)],
                   check=True, capture_output=True, timeout=120)
    r = lint(so)
    out = r.stdout + r.stderr
    assert r.returncode == (1 if flagged else 0), out
    for var in flagged:
        assert "STALE" in out and var in out, out
    if not flagged:
        assert "UNSAFE" not in out, out


@pytest.mark.parametrize("case", sorted(CASES))
def test_fixture_helper_cases(tmp_path, case):
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no C compiler")
    body, flagged = CASES[case]
    src = tmp_path / "fixture.c"
    src.write_text(PRELUDE + body)
    so = tmp_path / "fixture.so"
    subprocess.run([cc, "-O2", "-arch", "arm64", "-bundle", "-undefined",
                    "dynamic_lookup", "-o", str(so), str(src)],
                   check=True, capture_output=True, timeout=120)
    r = lint(so)
    out = r.stdout + r.stderr
    assert r.returncode == (1 if flagged else 0), out
    if flagged:
        assert "STALE" in out and "[rng]" in out, out
    else:
        assert "UNSAFE" not in out, out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__] + sys.argv[1:]))

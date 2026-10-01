#!/usr/bin/env python3
"""Check that a patched CPython build kept exec-home's thread-state reads intact.

exec-home routes every read of the `_Py_tss_tstate` thread-local through a call
into Python/pystate.c, so no caller elsewhere holds a read the compiler may cache
across a fiber park.  Cross-TU inlining (LTO) breaks that: it inlines pystate.c's
direct readers (PyThreadState_Get() and friends) into hundreds of functions.  This
counts the functions that read the thread-local directly and the call sites of
_PyThreadState_GetCurrent() / PyThreadState_Get(), and exits 1 when the direct
readers are not confined to a few dozen (pystate.c's own).

Measured (arm64 Darwin, clang 21, 3.14.4 + both patches): plain build 28 direct
readers, --enable-optimizations 17, --enable-optimizations --with-lto=thin 413.

arm64 Mach-O only (macOS TLV access pattern, `nm` + `otool`); exits 2 elsewhere.

usage: check_exec_home_tls.py [PYTHON_BINARY]   (default: this interpreter)
"""
import platform
import re
import subprocess
import sys
from collections import Counter

LIMIT = 64          # pystate.c's own readers number ~20-30; LTO builds hundreds


def main(argv):
    binary = argv[1] if len(argv) > 1 else sys.executable
    if sys.platform != "darwin" or platform.machine() != "arm64":
        print("check_exec_home_tls: only arm64 macOS is supported (got %s/%s)"
              % (sys.platform, platform.machine()))
        return 2
    syms = subprocess.run(["nm", binary], capture_output=True, text=True).stdout
    desc = None
    for line in syms.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "__Py_tss_tstate":
            desc = int(parts[0], 16)
    if desc is None:
        print("check_exec_home_tls: no _Py_tss_tstate TLV descriptor in %s "
              "(a shared-library build? point this at libpython instead)" % binary)
        return 2
    page, off = desc & ~0xFFF, desc & 0xFFF

    dis = subprocess.run(["otool", "-tV", binary], capture_output=True, text=True).stdout
    adrp_re = re.compile(r"\tadrp\t(x\d+), \d+ ; 0x([0-9a-f]+)")
    add_re = re.compile(r"\tadd\t(x\d+), (x\d+), #0x([0-9a-f]+)")
    func, pending = None, {}
    direct, calls = Counter(), Counter()
    for line in dis.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            func, pending = line[:-1], {}
            continue
        m = adrp_re.search(line)
        if m:
            pending[m.group(1)] = int(m.group(2), 16) == page
            continue
        m = add_re.search(line)
        if m and pending.get(m.group(2)) and int(m.group(3), 16) == off:
            direct[func] += 1
            continue
        m = re.search(r"\tbl?\t(__PyThreadState_GetCurrent|_PyThreadState_Get)$", line)
        if m:
            calls[m.group(1)] += 1

    print("%s" % binary)
    print("  functions reading _Py_tss_tstate directly: %d" % len(direct))
    for f, n in direct.most_common(6):
        print("      %-48s %d" % (f.lstrip("_"), n))
    print("  call sites: _PyThreadState_GetCurrent %d, PyThreadState_Get %d"
          % (calls["__PyThreadState_GetCurrent"], calls["_PyThreadState_Get"]))
    if len(direct) > LIMIT:
        print("UNSAFE: thread-state reads were inlined outside pystate.c "
              "(LTO?) -- see src/patches/README.md, 'Build flags'")
        return 1
    print("OK: direct reads confined to pystate.c")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

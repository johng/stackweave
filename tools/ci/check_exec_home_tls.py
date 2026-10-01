#!/usr/bin/env python3
"""Check that a patched CPython build kept exec-home's thread-state reads intact.

exec-home routes every read of the `_Py_tss_tstate` thread-local through a call
into Python/pystate.c, so no caller elsewhere holds a read the compiler may cache
across a fiber park.  Cross-TU inlining (LTO) breaks that: it inlines pystate.c's
direct readers (PyThreadState_Get() and friends) into hundreds of functions.

This disassembles the interpreter, finds every function that reads the
thread-local -- directly, or through a machine-outlined helper
(`_OUTLINED_FUNCTION_*`, matched by address: every object has its own helpers of
the same names) that does the read for it -- maps each to its object file through
the binary's debug map (`nm -pa`, OSO/FUN stabs, matched by address), and exits 1
if any reader is outside pystate.o (an unknown object counts as outside).
Without a debug map (stripped binary) it falls back to a count: more than a few
dozen readers means unsafe.

Measured (arm64 Darwin, clang 21, 3.14.4 + both patches): plain build and
--enable-optimizations keep every reader in pystate.o; --enable-optimizations
--with-lto=thin has ~400 readers outside it.

arm64 Mach-O only (macOS TLV access pattern, `nm` + `objdump`); exits 2 elsewhere.

usage: check_exec_home_tls.py [PYTHON_BINARY]   (default: this interpreter)
"""
import platform
import re
import subprocess
import sys
from collections import Counter

LIMIT = 64          # fallback only: pystate.c's own readers number ~20-30

FUNC_RE = re.compile(r"^([0-9a-f]+) <(.+)>:$")
ADRP_RE = re.compile(r"\tadrp\t(x\d+), 0x([0-9a-f]+)")
ADD_RE = re.compile(r"\tadd\t(x\d+), (x\d+), #0x([0-9a-f]+)")
CALL_RE = re.compile(r"\tbl?\t0x([0-9a-f]+) <([^>+]+)>$")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def tls_descriptor(binary):
    for line in run(["nm", binary]).splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "__Py_tss_tstate":
            return int(parts[0], 16)
    return None


def debug_map(binary):
    """{function start address: object basename} from the OSO/FUN stabs."""
    obj, funcs = None, {}
    for line in run(["nm", "-pa", binary]).splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        kind, name = parts[4], parts[5]
        if kind == "OSO":
            obj = name.rsplit("/", 1)[-1]
        elif kind == "FUN" and name and obj:
            funcs[int(parts[0], 16)] = obj
    return funcs


def main(argv):
    binary = argv[1] if len(argv) > 1 else sys.executable
    if sys.platform != "darwin" or platform.machine() != "arm64":
        print("check_exec_home_tls: only arm64 macOS is supported (got %s/%s)"
              % (sys.platform, platform.machine()))
        return 2
    desc = tls_descriptor(binary)
    if desc is None:
        print("check_exec_home_tls: no _Py_tss_tstate TLV descriptor in %s "
              "(a shared-library build? point this at libpython instead)" % binary)
        return 2
    page, off = desc & ~0xFFF, desc & 0xFFF

    names = {}                  # function address -> symbol name
    direct = set()              # addresses of functions that read the descriptor
    callers = {}                # callee address -> set of caller addresses
    calls = Counter()
    func, pending = None, {}
    for line in run(["objdump", "-d", "--no-show-raw-insn", binary]).splitlines():
        m = FUNC_RE.match(line)
        if m:
            func, pending = int(m.group(1), 16), {}
            names[func] = m.group(2)
            continue
        if func is None:
            continue
        m = ADRP_RE.search(line)
        if m:
            pending[m.group(1)] = int(m.group(2), 16) == page
            continue
        m = ADD_RE.search(line)
        if m and pending.get(m.group(2)) and int(m.group(3), 16) == off:
            direct.add(func)
            continue
        m = CALL_RE.search(line)
        if m:
            callers.setdefault(int(m.group(1), 16), set()).add(func)
            calls[m.group(2)] += 1

    # A machine-outlined helper that does the read stands for its callers.
    readers = set()
    for f in direct:
        if names.get(f, "").startswith("_OUTLINED_FUNCTION_"):
            readers |= callers.get(f, set())
        else:
            readers.add(f)

    print("%s" % binary)
    print("  functions reading _Py_tss_tstate (directly or via an outlined helper): %d"
          % len(readers))
    print("  call sites: _PyThreadState_GetCurrent %d, PyThreadState_Get %d"
          % (calls["__PyThreadState_GetCurrent"], calls["_PyThreadState_Get"]))
    objs = debug_map(binary)
    if objs:
        where = {f: objs.get(f, "<unknown object>") for f in readers}
        by_obj = Counter(where.values())
        print("  by object: %s" % ", ".join("%s %d" % kv for kv in by_obj.most_common(6)))
        outside = sorted((names.get(f, hex(f)), o) for f, o in where.items()
                         if o != "pystate.o")
        if outside:
            print("UNSAFE: %d reader(s) outside pystate.o (LTO?), e.g. %s -- see "
                  "src/patches/README.md, 'Build flags'"
                  % (len(outside), ", ".join("%s (%s)" % (n.lstrip("_"), o)
                                              for n, o in outside[:4])))
            return 1
        print("OK: every reader is in pystate.o")
        return 0
    print("  (no debug map: cannot attribute readers to files; using the count)")
    if len(readers) > LIMIT:
        print("UNSAFE: %d readers, more than pystate.c's own (LTO?) -- see "
              "src/patches/README.md, 'Build flags'" % len(readers))
        return 1
    print("OK by count only (%d <= %d); rebuild with -g for a definite answer"
          % (len(readers), LIMIT))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

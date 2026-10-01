#!/usr/bin/env python3
"""tsan_refcount_oracle.py SRC -- make wrong-thread ob_ref_local updates visible to TSan.

Run on a patched (alloc-home + exec-home) CPython 3.14 source tree before
configure; tools/build_tsan_cpython.sh does it under ORACLE=1 / ORACLE=plain-tid,
which also adds the defines below to pyconfig.h.  Diagnostic interpreters only.

WHY.  Free-threaded CPython updates ob_ref_local with a relaxed atomic load and
a relaxed atomic store, and TSan never reports atomic-vs-atomic, so a plain TSan
build is structurally blind to the one race the exec-home `_Py_ThreadId()` half
exists for: a stale thread id sending a refcount update down the owner-only,
non-atomic path from a thread that does not own the object.  Under
Py_TSAN_REFLOCAL_ORACLE:
  * the OWNER-path store becomes a plain (instrumented) store,
  * every read made BEFORE the ownership check (the immortality test, Py_REFCNT,
    _Py_IsImmortal, _Py_TryIncrefFast, _PyObject_ResurrectEnd) goes through an
    uninstrumented helper, so legitimate cross-thread reads stay silent.
Two owner-path stores from different threads with no happens-before between
them then report as a plain write/write race on ob_ref_local.  Teeth:
tools/verify/tsan_oracle_teeth.py.  Py_TSAN_ORACLE_PLAIN_TID additionally drops
exec-home's volatile on `_Py_ThreadId()`'s asm, to test whether that half is
load-bearing (it is: tools/verify/tsan_oracle_zip_canary.py, docs/dev/TSAN.md).

Edits are exact-match and counted; a CPython version that moved any of them
fails loudly instead of building a half-instrumented oracle.
"""
import re, sys
src = sys.argv[1]

def edit(path, pairs, count_check=True):
    p = src + "/" + path
    s = open(p).read()
    for old, new, n in pairs:
        c = s.count(old)
        if count_check and c != n:
            raise SystemExit("%s: expected %d of %r, found %d" % (path, n, old, c))
        s = s.replace(old, new)
    open(p, "w").write(s)

HELPER = r'''
#if defined(Py_GIL_DISABLED) && defined(Py_TSAN_REFLOCAL_ORACLE)
__attribute__((no_sanitize("thread"), noinline, unused))
static uint32_t _Py_oracle_peek_local(PyObject *op)
{
    return *(volatile uint32_t *)&op->ob_ref_local;
}
#  define _Py_REFLOCAL_PEEK(op) _Py_oracle_peek_local(_PyObject_CAST(op))
#  define _Py_REFLOCAL_OWNER_STORE(op, v) ((op)->ob_ref_local = (v))
#elif defined(Py_GIL_DISABLED)
#  define _Py_REFLOCAL_PEEK(op) _Py_atomic_load_uint32_relaxed(&(op)->ob_ref_local)
#  define _Py_REFLOCAL_OWNER_STORE(op, v) _Py_atomic_store_uint32_relaxed(&(op)->ob_ref_local, (v))
#endif

'''

# refcount.h: helper before _Py_REFCNT's block; every pre-ownership read -> PEEK,
# every owner-path store -> OWNER_STORE (all three stores here are owner-gated).
p = src + "/Include/refcount.h"
s = open(p).read()
if "_Py_REFLOCAL_PEEK" in s:
    raise SystemExit("%s: oracle already applied -- start from a fresh tree" % p)
anchor = "#if defined(Py_LIMITED_API) && Py_LIMITED_API+0 >= 0x030e0000\n    // Stable ABI implements Py_REFCNT()"
assert s.count(anchor) == 1, "anchor"
s = s.replace(anchor, HELPER + anchor)
n1 = s.count("_Py_atomic_load_uint32_relaxed(&ob->ob_ref_local)")
n2 = s.count("_Py_atomic_load_uint32_relaxed(&op->ob_ref_local)")
s = s.replace("_Py_atomic_load_uint32_relaxed(&ob->ob_ref_local)", "_Py_REFLOCAL_PEEK(ob)")
s = s.replace("_Py_atomic_load_uint32_relaxed(&op->ob_ref_local)", "_Py_REFLOCAL_PEEK(op)")
s, n3 = re.subn(r"_Py_atomic_store_uint32_relaxed\(&op->ob_ref_local, (\w+)\)",
                r"_Py_REFLOCAL_OWNER_STORE(op, \1)", s)
# 3.14.4: _Py_REFCNT reads via `ob`; _Py_IsImmortal, Py_INCREF and both Py_DECREF
# variants via `op`; three owner-path stores.  Any other count means CPython moved
# a refcount path and the oracle would silently miss it.
if (n1, n2, n3) != (1, 4, 3):
    raise SystemExit("%s: expected peek ob=1 op=4, owner stores=3; found %d/%d/%d"
                     % (p, n1, n2, n3))
open(p, "w").write(s)
print("refcount.h: peek ob=%d op=%d, owner stores=%d" % (n1, n2, n3))

# pycore_object.h: _Py_RefcntAdd store, _Py_TryIncrefFast, _PyObject_ResurrectEnd.
edit("Include/internal/pycore_object.h", [
    ("        _Py_atomic_store_uint32_relaxed(&op->ob_ref_local, (uint32_t)refcnt);",
     "        _Py_REFLOCAL_OWNER_STORE(op, (uint32_t)refcnt);", 1),
    ("_Py_TryIncrefFast(PyObject *op) {\n    uint32_t local = _Py_atomic_load_uint32_relaxed(&op->ob_ref_local);",
     "_Py_TryIncrefFast(PyObject *op) {\n    uint32_t local = _Py_REFLOCAL_PEEK(op);", 1),
    ("        _Py_atomic_store_uint32_relaxed(&op->ob_ref_local, local);\n#ifdef Py_REF_DEBUG\n        _Py_IncRefTotal",
     "        _Py_REFLOCAL_OWNER_STORE(op, local);\n#ifdef Py_REF_DEBUG\n        _Py_IncRefTotal", 1),
    ("    uint32_t local = _Py_atomic_load_uint32_relaxed(&op->ob_ref_local);\n    Py_ssize_t shared = _Py_atomic_load_ssize_acquire(&op->ob_ref_shared);",
     "    uint32_t local = _Py_REFLOCAL_PEEK(op);\n    Py_ssize_t shared = _Py_atomic_load_ssize_acquire(&op->ob_ref_shared);", 1),
    ("        _Py_atomic_store_uint32_relaxed(&op->ob_ref_local, 0);\n# ifdef Py_TRACE_REFS",
     "        _Py_REFLOCAL_OWNER_STORE(op, 0);\n# ifdef Py_TRACE_REFS", 1),
])
print("pycore_object.h edited")

# ceval.c's own Py_DECREF macro.
edit("Python/ceval.c", [
    ("        uint32_t local = _Py_atomic_load_uint32_relaxed(&op->ob_ref_local); \\",
     "        uint32_t local = _Py_REFLOCAL_PEEK(op); \\", 1),
    ("            _Py_atomic_store_uint32_relaxed(&op->ob_ref_local, local); \\",
     "            _Py_REFLOCAL_OWNER_STORE(op, local); \\", 1),
])
print("ceval.c edited")

# Optional: exec-home without the volatile _Py_ThreadId() half.
edit("Include/object.h", [
    ("#ifdef Py_TSTATE_EXEC_HOME\n#  define _Py_TID_ASM __asm__ __volatile__",
     "#if defined(Py_TSTATE_EXEC_HOME) && !defined(Py_TSAN_ORACLE_PLAIN_TID)\n#  define _Py_TID_ASM __asm__ __volatile__", 1),
])
print("object.h edited")

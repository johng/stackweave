#!/usr/bin/env bash
# build_tsan_cpython.sh -- build a free-threaded CPython instrumented with
# ThreadSanitizer, the "gold standard" interpreter for TSan-ing stackweave (CPython's
# own internals are then instrumented too, so races that cross the
# ext <-> interpreter boundary are attributed precisely).  Linux and macOS.
#
# CPython supports this directly: ./configure --disable-gil
# --with-thread-sanitizer, and ships Tools/tsan/suppressions_free_threading.txt.
#
# BOTH MIGRATION PATCHES ARE APPLIED (src/patches/, alloc-home + exec-home), with
# Py_TSTATE_ALLOC_HOME / Py_TSTATE_EXEC_HOME in CPPFLAGS and appended to
# pyconfig.h -- the interpreter stackweave is actually sound on.  A gold run on a
# stock interpreter would TSan the migration crashes those patches prevent, not
# stackweave.  No --with-lto (it undoes exec-home, src/patches/README.md) and no
# --enable-optimizations (PGO's training run is pointless under TSan).
#
# THE CRITICAL LINUX GOTCHA (cost a day): `configure` must ALSO run under
# `setarch -R`.  With --with-thread-sanitizer, configure's own AC_RUN_IFELSE
# feature-probe binaries are compiled with -fsanitize=thread.  On Linux 6.x's
# high-entropy ASLR every TSan binary aborts at startup ("unexpected memory
# mapping"), so each runtime probe "fails" and configure bakes garbage into
# pyconfig.h -- notably SIZEOF_WCHAR_T=0 and WORDS_BIGENDIAN=1 on a little-endian
# x86-64 (the "character U+6f006e is not in range" getpath crash).  Running
# configure under setarch -R disables ASLR for those probe binaries too.  macOS
# has no such problem (and no setarch).
#
# WHICH VERSION YOU BUILD IS PART OF THE RESULT.  RUNLOOM_GCFRAMES_ANCHOR is
# gated `Py_GIL_DISABLED && PY_VERSION_HEX >= 0x030E0000`, so a 3.13 gold run
# never sees the anchor (that is how an earlier "TSan-clean" claim went stale).
# Build the version you ship.  The patches exist for 3.14 and 3.15.
#
# ORACLE=1 additionally makes wrong-thread ob_ref_local updates visible to TSan
# (tools/verify/tsan_refcount_oracle.py; docs/dev/TSAN.md "The ob_ref_local
# oracle"); ORACLE=plain-tid also drops exec-home's volatile on _Py_ThreadId().
# Both are diagnostic interpreters only -- never ship them.
#
# Usage:  tools/build_tsan_cpython.sh [VERSION]
# Env:    PY_VER (default 3.14.4), SRC_DIR, PREFIX, JOBS, ORACLE (unset|1|plain-tid),
#         PY_TARBALL (a local copy of the pinned tarball; still hash-checked)
# Takes ~4 min on an 18-core M-series Mac (make -j18), longer on Linux.
set -euo pipefail

VER="${1:-${PY_VER:-3.14.4}}"
SRC="${SRC_DIR:-$HOME/projects/cpython-tsan}"
PREFIX="${PREFIX:-$HOME/cpython-tsan}"
ORACLE="${ORACLE:-}"
RM="$(command -v safe-rm || echo rm)"
_RLT="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$_RLT")"
JOBS="${JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)}"

SA=""
if [ "$(uname -s)" = Linux ]; then
    command -v setarch >/dev/null 2>&1 && SA="setarch $(uname -m) -R"
    [ -z "$SA" ] && echo "WARNING: setarch not found; TSan binaries abort under ASLR on 6.x"
fi

MAJMIN="${VER%.*}"                       # 3.14
PATCHTAG="cpython${MAJMIN//./}t"         # cpython314t
P_ALLOC="$ROOT/src/patches/$PATCHTAG-tstate-alloc-home.patch"
P_EXEC="$ROOT/src/patches/$PATCHTAG-tstate-exec-home.patch"
[ -f "$P_ALLOC" ] && [ -f "$P_EXEC" ] || { echo "no migration patches for $MAJMIN in src/patches/"; exit 1; }

# Clean tree: an ASLR-aborted partial build leaves corrupt frozen-module
# headers a resume won't regenerate.
# Pinned fetch: this tarball becomes the interpreter every sanitizer result
# below is judged against, so an unverified download would silently undermine
# every finding. Unpinned versions are refused, not fetched.
. "$_RLT/cpython_pins.env"
. "$_RLT/fetch_pinned.sh"
PY_SHA256="$(rl_cpython_require_pin "$VER")" || exit 1
TGZ="${PY_TARBALL:-${TMPDIR:-/tmp}/py-$VER.tgz}"
# `|| rc=$?`: under set -e a bare failing call would exit before the case.
rc=0
rl_fetch_pinned "https://www.python.org/ftp/python/$VER/Python-$VER.tgz" \
                "$PY_SHA256" "$TGZ" || rc=$?
case $rc in
    0) : ;;
    1) echo "could not download CPython $VER (offline?)" >&2; exit 1 ;;
    2) exit 1 ;;
esac
$RM -rf "$SRC"
mkdir -p "$SRC"
tar xzf "$TGZ" -C "$SRC" --strip-components=1
cd "$SRC"

patch -p1 -F0 --forward < "$P_ALLOC" >/dev/null
patch -p1 -F0 --forward < "$P_EXEC" >/dev/null
[ -z "$(find . -name '*.rej')" ] || { echo "FATAL: a migration patch did not apply cleanly"; exit 1; }
grep -q _PyThreadStateImpl_AllocHome Include/internal/pycore_tstate.h \
    || { echo "FATAL: alloc-home witness missing"; exit 1; }
grep -rqs _Py_TID_ASM Include/object.h Include/cpython/object.h \
    || { echo "FATAL: exec-home witness missing"; exit 1; }
case "$ORACLE" in
    "") ;;
    1|plain-tid) python3 "$ROOT/tools/verify/tsan_refcount_oracle.py" "$SRC" ;;
    *) echo "ORACLE must be unset, 1 or plain-tid"; exit 1 ;;
esac

EXTRA_CONF=()
if [ "$(uname -s)" = Darwin ] && command -v brew >/dev/null 2>&1 \
        && OSSL="$(brew --prefix openssl@3 2>/dev/null)" && [ -d "$OSSL" ]; then
    EXTRA_CONF+=(--with-openssl="$OSSL")
fi
# Run configure UNDER setarch -R on Linux (see header).  ac_cv_buggy_getaddrinfo=no
# additionally skips the one network probe.
$SA env ac_cv_buggy_getaddrinfo=no \
    ./configure --disable-gil --with-thread-sanitizer --prefix="$PREFIX" \
    ${EXTRA_CONF[@]+"${EXTRA_CONF[@]}"} \
    CPPFLAGS="-DPy_TSTATE_ALLOC_HOME -DPy_TSTATE_EXEC_HOME ${CPPFLAGS:-}" >/dev/null
# configure's CPPFLAGS reach the interpreter's own compiles but not extensions;
# pyconfig.h is what an extension sees, so the defines go there as well.
{
    printf '#define Py_TSTATE_ALLOC_HOME 1\n#define Py_TSTATE_EXEC_HOME 1\n'
    [ -n "$ORACLE" ] && printf '#define Py_TSAN_REFLOCAL_ORACLE 1\n'
    [ "$ORACLE" = plain-tid ] && printf '#define Py_TSAN_ORACLE_PLAIN_TID 1\n'
    true
} >> pyconfig.h

# Sanity-check the detection that the ASLR-abort used to corrupt, before a long
# build: bail loudly if configure still mis-detected (e.g. setarch unavailable).
wsz="$(sed -n 's/^#define SIZEOF_WCHAR_T //p' pyconfig.h)"
if [ "$wsz" != 4 ]; then
    echo "FATAL: configure mis-detected SIZEOF_WCHAR_T=$wsz (expected 4)."
    echo "       configure's TSan probes likely aborted under ASLR -- need setarch -R."
    exit 1
fi
# Robust endianness check: a naive `grep WORDS_BIGENDIAN pyconfig.h` FALSE-POSITIVES
# on every little-endian build, because autoconf's AC_C_BIGENDIAN template always
# emits the macro name in a comment, in an inactive Apple-universal-build
# `#  define WORDS_BIGENDIAN 1` branch, and in a `/* #undef */` line.  PREPROCESS
# pyconfig.h instead and test whether the macro is ACTUALLY defined on this host.
if printf '#include "pyconfig.h"\n#ifdef WORDS_BIGENDIAN\nRUNLOOM_BIG_ENDIAN\n#endif\n' \
     | cc -I. -E -P - 2>/dev/null | grep -q RUNLOOM_BIG_ENDIAN; then
    echo "FATAL: WORDS_BIGENDIAN actively defined on a little-endian host -- ASLR-probe corruption."; exit 1
fi

$SA make -j"$JOBS" >/dev/null
$SA make install >/dev/null

PY="$PREFIX/bin/python${MAJMIN}t"; [ -x "$PY" ] || PY="$PREFIX/bin/python3"
$SA "$PY" -c 'import sys; print(sys.version); print("GIL on:", sys._is_gil_enabled())'
$SA "$PY" -m ensurepip >/dev/null 2>&1 || true
$SA "$PY" -m pip install -q pytest setuptools 2>&1 | tail -1 || true
echo "TSan interpreter:  $PY"
echo "CPython TSan supp: $SRC/Tools/tsan/suppressions_free_threading.txt"

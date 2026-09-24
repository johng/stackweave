#!/usr/bin/env bash
# build_pydebug_cpython.sh -- build a --with-pydebug --disable-gil free-threaded
# CPython (in-tree, no install).  This is the OBJECT-SEAM assertion lens: CPython's
# own internal asserts -- tstate ownership (pystate.c), gilstate, biased-refcount
# merge, mimalloc heap->thread binding -- fire at the EXACT source line that a
# release build would silently corrupt (the class every recent stackweave seam bug
# lived on).  A --with-pydebug build also exposes sys.gettotalrefcount(), the
# net-zero refleak oracle (tools/refleak_hunt.py).
#
# Unlike build_tsan_cpython.sh there is NO sanitizer instrumentation, so NO
# setarch/ASLR dance is needed.  Point STACKWEAVE_PYDEBUG_PYTHON (tools/run_pydebug.sh)
# or SEAM_PYTHON (tools/seamfuzz) at the resulting <SRC>/python.
#
# Usage:  tools/build_pydebug_cpython.sh [VERSION]
# Env:    PY_VER (default 3.14.4), SRC_DIR (default ~/projects/cpython-pydebug)
set -euo pipefail

VER="${1:-${PY_VER:-3.14.4}}"
SRC="${SRC_DIR:-$HOME/projects/cpython-pydebug}"
RM="$(command -v safe-rm || echo rm)"

# Pinned fetch: this tarball becomes the interpreter every sanitizer result
# below is judged against, so an unverified download would silently undermine
# every finding. Unpinned versions are refused, not fetched.
_RLT="$(cd "$(dirname "$0")" && pwd)"
. "$_RLT/cpython_pins.env"
. "$_RLT/fetch_pinned.sh"
PY_SHA256="$(rl_cpython_require_pin "$VER")" || exit 1
rl_fetch_pinned "https://www.python.org/ftp/python/$VER/Python-$VER.tgz" \
                "$PY_SHA256" "/tmp/py-$VER.tgz"
case $? in
    0) : ;;
    1) echo "could not download CPython $VER (offline?)" >&2; exit 1 ;;
    2) exit 1 ;;
esac
$RM -rf "$SRC"
tar xzf /tmp/py-$VER.tgz -C "$(dirname "$SRC")"
mv "$(dirname "$SRC")/Python-$VER" "$SRC"
cd "$SRC"

# in-tree build (run_pydebug.sh points at <SRC>/python directly, no install).
env ac_cv_buggy_getaddrinfo=no \
    ./configure --with-pydebug --disable-gil >/dev/null

make -j"$(nproc)"

PY="$SRC/python"
"$PY" -c 'import sys; print(sys.version); print("GIL enabled:", sys._is_gil_enabled()); \
print("pydebug (gettotalrefcount):", hasattr(sys, "gettotalrefcount"))'
"$PY" -m ensurepip >/dev/null 2>&1 || true
"$PY" -m pip install -q pytest hypothesis 2>&1 | tail -1 || true
echo "PYDEBUG-BUILD-OK: $PY"

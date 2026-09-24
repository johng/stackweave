#!/usr/bin/env bash
# setup_dredd.sh -- fetch the dredd schemata mutation tool + make it runnable on
# this box.  dredd (github.com/mc-imperial/dredd) rewrites a C/C++ source so EVERY
# mutation point becomes a runtime-selectable branch (env DREDD_ENABLED_MUTATION),
# compiled ONCE -- so an exhaustive mutation sweep costs one build + N test runs,
# not N builds.  That is what makes mutation testing the stackweave C core tractable.
#
# Installs into tools/mutate/schemata/dredd/ (gitignored -- it's a ~100 MB LLVM
# binary, versioned by download, not committed).  Idempotent.
#
# Prereqs handled: the ubuntu-24.04 release binary links libLLVM-17 (we ship
# clang-18), so we apt-install the 17 runtime libs; and dredd bundles LLVM-17's
# clang but not its builtin headers (limits.h ...), so callers pass clang-18's
# resource-dir via -isystem $(clang-18 -print-resource-dir)/include (see
# build_target.sh).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HERE/dredd"
REL="https://github.com/mc-imperial/dredd/releases/download/1.0/dredd-ubuntu-24.04-Release.zip"
REL_SHA256="79c3e38c786883eff424b550d637bfee4be2a882ebcfe49038233eac07dd708b"

if [ -x "$DEST/dredd/bin/dredd" ] && "$DEST/dredd/bin/dredd" --help >/dev/null 2>&1; then
  echo "dredd already runnable: $DEST/dredd/bin/dredd"; exit 0
fi

echo "=== LLVM-17 runtime libs (dredd 1.0 release links libLLVM-17) ==="
sudo -n apt-get install -y -q libllvm17t64 libclang-cpp17t64 2>/dev/null \
  || sudo -n apt-get install -y -q libllvm17 libclang-cpp17 2>/dev/null \
  || echo "WARN: could not apt-install LLVM-17 libs -- dredd may fail to load libLLVM-17.so.1"

echo "=== download + unpack dredd 1.0 release ==="
mkdir -p "$DEST"
# Pinned: this unpacks an executable that then rewrites C source for mutation
# testing. Trust-on-first-use -- mc-imperial/dredd publishes no checksums.
# See tools/fetch_pinned.sh.
. "$(cd "$(dirname "$0")/../../.." && pwd)/tools/fetch_pinned.sh"
rl_fetch_pinned "$REL" "$REL_SHA256" "$DEST/dredd.zip"
case $? in
    0) : ;;
    1) echo "download failed"; exit 1 ;;
    2) echo "dredd release FAILED its pin -- refusing to unpack it."; exit 1 ;;
esac
unzip -oq "$DEST/dredd.zip" -d "$DEST" && rm -f "$DEST/dredd.zip"
chmod +x "$DEST/dredd/bin/dredd" 2>/dev/null

echo "=== verify ==="
if "$DEST/dredd/bin/dredd" --help >/dev/null 2>&1; then
  echo "OK: $DEST/dredd/bin/dredd"
else
  echo "FAIL: dredd will not run -- check 'ldd $DEST/dredd/bin/dredd' for missing libLLVM-17.so.1"
  exit 1
fi

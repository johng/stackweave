#!/usr/bin/env sh
# collect_wheels.sh -- build wheels for EVERY platform from a clean, configurable
# git checkout and gather them, plus the sdist, into ./dist for one
# `twine upload dist/*`.
#
# No hosted CI: this orchestrates cibuildwheel locally (this machine's platform)
# and over SSH on your other build hosts (e.g. a Mac), then copies their
# wheels back here.  cibuildwheel cannot cross-build macOS from Linux, so each
# OS builds its own -- this just automates the shovelling.
#
# Every host builds from a FRESH `git clone` (of a configurable repo URL + ref)
# in a unique throwaway dir that is deleted on exit -- wheels always come from
# exactly the committed code you intend to release, never a mutated/stale tree.
#
# Configure in an UNTRACKED file (never committed): scripts/release_hosts.env
# (copy scripts/release_hosts.env.example):
#   STACKWEAVE_REPO_URL   git URL to build from   (PROMPTED if unset)
#   STACKWEAVE_REF        branch/tag/sha to build (default: main)
#   RELEASE_SSH_HOSTS  space-separated "<target>|<base-dir>" entries, one per
#                      non-Linux platform.  Each host needs git, a C toolchain,
#                      the CPython matrix, and cibuildwheel>=2.20.
#
# Usage:  ./scripts/collect_wheels.sh            (build + collect, twine check)
#         ./scripts/collect_wheels.sh --upload   (...then twine upload dist/*)
set -eu

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="${PYTHON:-python3}"

UPLOAD=no
[ "${1:-}" = "--upload" ] && UPLOAD=yes

if [ -f scripts/release_hosts.env ]; then
    # shellcheck disable=SC1091
    . scripts/release_hosts.env
fi

# Prompt for anything missing (no TTY -> silent default, so automation never hangs).
ask() {   # ask VAR "prompt" "default"
    eval "cur=\${$1:-}"; [ -n "$cur" ] && return
    def="$3"; ans=""
    [ -t 0 ] && { printf '%s [%s]: ' "$2" "$def" >&2; read -r ans || true; }
    [ -z "$ans" ] && ans="$def"
    eval "$1=\$ans"
}
ask STACKWEAVE_REPO_URL "Repo URL to build from" "https://github.com/johng/stackweave.git"
ask STACKWEAVE_REF      "Ref to build (branch/tag/sha)" "main"
[ -z "${RELEASE_SSH_HOSTS+set}" ] && ask RELEASE_SSH_HOSTS \
    "Remote build hosts 'target|dir ...' (blank = this machine only)" ""
: "${RELEASE_SSH_HOSTS:=}"

STAMP="$(date +%Y%m%d-%H%M%S)-$$"
LOCAL_WORK=""
REMOTE_CLEANUP=""   # newline-separated "<target>|<dir>"

cleanup() {
    [ -n "$LOCAL_WORK" ] && rm -rf "$LOCAL_WORK" 2>/dev/null || true
    printf '%s\n' "$REMOTE_CLEANUP" | while IFS='|' read -r t d; do
        [ -z "$t" ] && continue
        ssh "$t" "rm -rf '$d'" >/dev/null 2>&1 || true
    done
}
trap cleanup EXIT INT TERM

echo ">> repo: $STACKWEAVE_REPO_URL   ref: $STACKWEAVE_REF"
rm -rf wheelhouse dist
mkdir -p wheelhouse dist

# ---- local platform (Linux: manylinux via docker; QEMU for aarch64) ------
LOCAL_WORK="$(mktemp -d)"
echo ">> [local: $(uname -s)] fresh clone + cibuildwheel ..."
git clone --depth 1 --branch "$STACKWEAVE_REF" "$STACKWEAVE_REPO_URL" "$LOCAL_WORK/src"
( cd "$LOCAL_WORK/src" && "$PY" -m cibuildwheel --output-dir "$ROOT/wheelhouse" )

# ---- remote platforms (e.g. a Mac) over SSH ------------------------------
for entry in $RELEASE_SSH_HOSTS; do
    target=$(printf '%s' "$entry" | cut -d'|' -f1)
    base=$(printf '%s' "$entry" | cut -d'|' -f2)
    bdir="$base/runloom-build-$STAMP"
    REMOTE_CLEANUP="$REMOTE_CLEANUP
$target|$bdir"
    echo ">> [$target] fresh clone $STACKWEAVE_REF + cibuildwheel ..."
    ssh "$target" "rm -rf '$bdir' && git clone --depth 1 --branch '$STACKWEAVE_REF' '$STACKWEAVE_REPO_URL' '$bdir' && cd '$bdir' && python3 -m cibuildwheel --output-dir wheelhouse"
    echo ">> [$target] copying wheels back ..."
    tmp_pull="$(mktemp -d)"
    scp -rq "$target:$bdir/wheelhouse" "$tmp_pull/"
    cp "$tmp_pull"/wheelhouse/*.whl wheelhouse/
    rm -rf "$tmp_pull"
done

# ---- sdist (built from the same fresh checkout) + gather -----------------
echo ">> building sdist from $STACKWEAVE_REF ..."
( cd "$LOCAL_WORK/src" && "$PY" -m build --sdist --outdir "$ROOT/dist" )
cp wheelhouse/*.whl dist/

echo ">> twine check ..."
"$PY" -m twine check dist/*

n_whl=$(find dist -name '*.whl' | wc -l | tr -d ' ')
echo
echo ">> dist/ ready: ${n_whl} wheels + 1 sdist (from $STACKWEAVE_REF). Throwaway clones cleaned."
if [ "$UPLOAD" = yes ]; then
    "$PY" -m twine upload dist/*
else
    echo "   upload with:  $PY -m twine upload dist/*    (or re-run with --upload)"
fi

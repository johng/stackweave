#!/usr/bin/env sh
# bench.sh -- run the runtime comparison (bench.compare: stackweave per
# feature config vs threads / asyncio / uvloop / trio / gevent / Go) into
# benchmark/bench/results/compare/<stamp>/, and optionally gate stackweave's
# default config against an earlier run of the same box.
#
# Usage:
#   scripts/bench.sh                              # every config + runtime, 2 passes
#   scripts/bench.sh --quick --passes 1           # args go to bench.compare
#   STACKWEAVE_BENCH_BASE=benchmark/bench/results/compare/<old> scripts/bench.sh
#                                                 # + gate default vs that run
#
#   PYTHON                      patched free-threaded 3.14t that runs stackweave
#                               (default ~/.pyenv/versions/3.14.4t-mig/bin/python3.14t)
#   STACKWEAVE_BASELINE_PYTHON  stock free-threaded interpreter with uvloop, trio
#                               and gevent installed (default: $PYTHON)
#   STACKWEAVE_GIL_PYTHON       a GIL build, for --runtimes asyncio-gil,uvloop-gil
#   STACKWEAVE_BENCH_TOL        gate tolerance on min_s (default 0.15)
#
# Compare runs of the same day on the same box: cross-day deltas on a shared
# or laptop box are mostly noise (see benchmark/README.md).
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
PY="${PYTHON:-$HOME/.pyenv/versions/3.14.4t-mig/bin/python3.14t}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="benchmark/bench/results/compare/$STAMP"
TOL="${STACKWEAVE_BENCH_TOL:-0.15}"
RUN="env PYTHONPATH=src:benchmark PYTHON_GIL=0"
SETARCH=""
command -v setarch >/dev/null 2>&1 && SETARCH="setarch -R"   # ASLR off on Linux

WRAP=""
command -v caffeinate >/dev/null 2>&1 && WRAP="caffeinate -i"  # macOS: no sleep mid-run
$WRAP $SETARCH $RUN "$PY" -m bench.compare --out-dir "$OUT" "$@"

BASE="${STACKWEAVE_BENCH_BASE:-}"
[ -n "$BASE" ] || exit 0
rc=0
for suite in mnsched echo; do
    old="$BASE/$suite-cur-default-p0.json"
    new="$OUT/$suite-cur-default-p0.json"
    [ -f "$old" ] && [ -f "$new" ] || continue
    printf '\n## %s regression gate vs %s (min_s, tol %s)\n' "$suite" "$BASE" "$TOL"
    $RUN "$PY" -m bench.regress "$old" "$new" --metric min_s --tol "$TOL" || rc=1
done
if [ "$rc" = 1 ]; then
    printf 'PERF GATE: regression detected\n'
    exit 1
fi
printf 'PERF GATE: ok\n'

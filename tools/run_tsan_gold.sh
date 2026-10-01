#!/usr/bin/env bash
# run_tsan_gold.sh -- the GOLD ThreadSanitizer lane, migration on (docs/dev/TSAN.md).
#
# Builds stackweave_c with STACKWEAVE_TSAN=1 against a --with-thread-sanitizer,
# free-threaded CPython that carries BOTH src/patches/ halves
# (tools/build_tsan_cpython.sh), proves the lane has teeth, then runs test files
# ONE AT A TIME under TSan and prints every report's SUMMARY site, marked KNOWN
# (triaged in tools/verify/tsan_gold_known.txt) or NEW.
#
# Nothing in src/runloom_c is suppressed (tools/tsan_suppressions.txt says why):
# triaged-benign races stay visible as KNOWN instead of being hidden.  The only
# suppressions are CPython's own free-threading list.
#
# Linux and macOS.  On Linux, TSan aborts under high-entropy ASLR, so every
# command runs under `setarch -R` (see build_tsan_cpython.sh).
#
# Usage:  STACKWEAVE_TSAN_PYTHON=/path/to/tsan/bin/python3.14t \
#         STACKWEAVE_TSAN_CPYTHON_SUPP=/path/to/Python-3.14.4/Tools/tsan/suppressions_free_threading.txt \
#         tools/run_tsan_gold.sh [test_file.py ...]
# Env:    TSAN_GOLD_TIMEOUT  per-file wall-clock budget in seconds (default 900)
#         TSAN_GOLD_EXTENDED=1  add the slow files (test_swarm_mn_sched, ~8 min under TSan)
#         KEEP_TSAN_SO=1     leave the instrumented .so in src/ (default: rebuild
#                            a normal .so with $PYTHON at the end, if PYTHON is set)
# Exit:   0 no NEW reports; 1 NEW reports; 2 the lane is broken (build or teeth).
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${STACKWEAVE_TSAN_PYTHON:-}"
[ -n "$PY" ] && [ -x "$PY" ] || { echo "set STACKWEAVE_TSAN_PYTHON to a --with-thread-sanitizer python (tools/build_tsan_cpython.sh)"; exit 2; }
"$PY" -c 'import sysconfig,sys; a=sysconfig.get_config_var("CONFIG_ARGS") or ""; sys.exit(0 if "--with-thread-sanitizer" in a and sysconfig.get_config_var("Py_GIL_DISABLED") else 1)' \
    || { echo "$PY is not a free-threaded --with-thread-sanitizer build"; exit 2; }
KNOWN="$ROOT/tools/verify/tsan_gold_known.txt"
TIMEOUT="${TSAN_GOLD_TIMEOUT:-900}"
RM="$(command -v safe-rm || echo rm)"
SA=""
if [ "$(uname -s)" = Linux ]; then
    command -v setarch >/dev/null 2>&1 && SA="setarch $(uname -m) -R" \
        || echo "WARNING: setarch not found; TSan aborts under ASLR on Linux 6.x"
fi

SUPP="$(mktemp "${TMPDIR:-/tmp}/sw_tsan_supp.XXXXXX")"
: > "$SUPP"
if [ -n "${STACKWEAVE_TSAN_CPYTHON_SUPP:-}" ] && [ -f "$STACKWEAVE_TSAN_CPYTHON_SUPP" ]; then
    cat "$STACKWEAVE_TSAN_CPYTHON_SUPP" >> "$SUPP"
else
    echo "WARNING: STACKWEAVE_TSAN_CPYTHON_SUPP unset -- CPython's known free-threading races will show as NEW"
fi
LOGDIR="$(mktemp -d "${TMPDIR:-/tmp}/sw_tsan_gold.XXXXXX")"

echo "================ stackweave GOLD TSan lane ================"
echo "  python : $PY"
echo "  logs   : $LOGDIR"

echo "-- building the instrumented extension (STACKWEAVE_TSAN=1) --"
$RM -f src/stackweave_c*.so
$RM -rf build/temp.tsan
if ! $SA env STACKWEAVE_TSAN=1 \
        STACKWEAVE_EXTRA_CFLAGS="-DPy_TSTATE_ALLOC_HOME -DPy_TSTATE_EXEC_HOME ${STACKWEAVE_EXTRA_CFLAGS:-}" \
        "$PY" setup.py build_ext --inplace --force --build-temp build/temp.tsan \
        > "$LOGDIR/build.log" 2>&1; then
    echo "  BUILD FAILED -- $LOGDIR/build.log"; tail -20 "$LOGDIR/build.log"; exit 2
fi
SO="$(ls src/stackweave_c*.so)"
if [ "$(uname -s)" = Darwin ]; then otool -L "$SO"; else ldd "$SO"; fi 2>/dev/null | grep -q tsan \
    || { echo "  $SO does not link the TSan runtime"; exit 2; }

# exitcode=0: a test judges its own result; races are read from the logs.  (It
# also makes TSan's own fatal reports exit 0 -- see docs/dev/TSAN.md for the
# one test that trips on that, the deliberate hub guard-page overflow.)
# history_size=7: the maximum; smaller values leave the OTHER access of a hub-
# thread race with a one-frame, unsymbolised stack.
# abort_on_error=0: macOS defaults it on, which turns the exit-time report into
# SIGABRT (and a slow crash report) for every process that saw a race.
tsan_opts() {
    echo "halt_on_error=0:abort_on_error=0:exitcode=0:report_signal_unsafe=0:history_size=7:suppressions=$SUPP:log_path=$1/tsan"
}
run() {  # label, cmd...
    local label="$1"; shift
    local d="$LOGDIR/$label"; mkdir -p "$d"
    local t0; t0=$(date +%s)
    $SA env PYTHON_GIL=0 PYTHONPATH="$ROOT/src" TSAN_OPTIONS="$(tsan_opts "$d")" \
        perl -e 'alarm shift; exec @ARGV' "$TIMEOUT" "$@" > "$d/out.txt" 2>&1
    local rc=$?
    local n; n=$(cat "$d"/tsan.* 2>/dev/null | grep -c '^WARNING: ThreadSanitizer')
    printf -- "-- %-34s rc=%-3s %4ss  reports=%s  | %s\n" "$label" "$rc" "$(( $(date +%s) - t0 ))" "$n" \
        "$(grep -v '^\s*$' "$d/out.txt" | tail -1 | cut -c1-70)"
}

echo "-- teeth (tools/verify/tsan_teeth.py) --"
run teeth_race  "$PY" tools/verify/tsan_teeth.py race
run teeth_clean "$PY" tools/verify/tsan_teeth.py clean
teeth_ok=1
grep -q "cross-hub" "$LOGDIR/teeth_race/out.txt" || { echo "  teeth: race writers never ran on two hubs"; teeth_ok=0; }
cat "$LOGDIR"/teeth_race/tsan.* 2>/dev/null | grep -q "SUMMARY: ThreadSanitizer: data race" \
    || { echo "  teeth: planted cross-hub race NOT reported (no instrumentation, or merged fiber histories)"; teeth_ok=0; }
if cat "$LOGDIR"/teeth_clean/tsan.* 2>/dev/null | grep -q "SUMMARY: ThreadSanitizer"; then
    echo "  teeth: clean control reported a race (lost happens-before across park/migration)"; teeth_ok=0
fi
[ "$teeth_ok" = 1 ] || { echo "  LANE BROKEN -- see $LOGDIR/teeth_*"; exit 2; }
echo "  teeth OK: planted race reported, clean control silent"

if [ $# -gt 0 ]; then
    FILES="$*"
else
    FILES="tests/test_mn.py tests/test_local_wake.py tests/test_cross_hub_migration.py
           tests/test_tlbc_parked_frame_gc.py tests/test_fiber_tstate_isolation.py
           tests/test_chan.py tests/test_chan_stress.py tests/test_sync.py
           tests/test_sync_primitives.py tests/test_mn_park.py tests/test_mn_teardown.py"
    [ "${TSAN_GOLD_EXTENDED:-0}" = 1 ] && FILES="$FILES tests/test_swarm_mn_sched.py"
fi
echo "-- test files under TSan (one process each, ${TIMEOUT}s budget) --"
for f in $FILES; do
    run "$(basename "$f" .py)" "$PY" -m pytest "$f" -q -p no:cacheprovider --no-header
done

echo "----------------------------------------------------------------"
echo "  race reports by SUMMARY site (KNOWN = triaged in $(basename "$KNOWN")):"
new=0
# The teeth's planted race is not a finding: summarise the test files only.
summaries="$(ls -d "$LOGDIR"/*/ | grep -v '/teeth_' | while read -r d; do cat "$d"tsan.* 2>/dev/null; done \
             | grep '^SUMMARY: ThreadSanitizer' \
             | sed 's/^SUMMARY: ThreadSanitizer: //' | sort | uniq -c | sort -rn)"
if [ -z "$summaries" ]; then
    echo "    none"
else
    while IFS= read -r line; do
        # KNOWN key = a suffix of "data race <file>:<line> in <function>".
        id="$(awk -F' *[|] *' -v s="$line" '
            /^[[:space:]]*(#|$)/ { next }
            { k = $1; sub(/[[:space:]]+$/, "", k)
              if (length(s) >= length(k) && substr(s, length(s) - length(k) + 1) == k) { print $2; exit } }' \
            "$KNOWN" 2>/dev/null)"
        if [ -n "$id" ]; then printf "    KNOWN %-6s %s\n" "$id" "$line"
        else printf "    NEW          %s\n" "$line"; new=1; fi
    done <<< "$summaries"
fi
echo "  logs: $LOGDIR"
echo "================================================================"

if [ "${KEEP_TSAN_SO:-0}" != 1 ] && [ -n "${PYTHON:-}" ]; then
    echo "-- restoring a normal extension with $PYTHON --"
    $RM -f src/stackweave_c*.so
    env STACKWEAVE_EXTRA_CFLAGS="-DPy_TSTATE_ALLOC_HOME -DPy_TSTATE_EXEC_HOME" \
        "$PYTHON" setup.py build_ext --inplace --force > "$LOGDIR/restore.log" 2>&1 \
        && echo "  normal .so restored" || echo "  WARNING: restore failed ($LOGDIR/restore.log)"
fi
exit "$new"

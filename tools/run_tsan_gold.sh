#!/usr/bin/env bash
# run_tsan_gold.sh -- the GOLD ThreadSanitizer lane, migration on (docs/dev/TSAN.md).
#
# Builds stackweave_c with STACKWEAVE_TSAN=1 against a --with-thread-sanitizer,
# free-threaded CPython that carries BOTH src/patches/ halves
# (tools/build_tsan_cpython.sh), proves the lane has teeth, then runs test files
# ONE AT A TIME under TSan and prints every report's SUMMARY site, marked KNOWN
# (triaged in tools/verify/tsan_gold_known.txt) or NEW.
#
# The checkout is never modified.  The working tree (tracked files plus untracked,
# non-ignored ones) is copied to $LOGDIR/tree and built and run there -- several
# tests put "src" first on sys.path in their subprocesses, so a TSan .so placed
# anywhere else would be silently shadowed by the checkout's normal one.  The
# instrumented tree stays in the log directory for rerunning a test by hand.
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
#         tools/run_tsan_gold.sh [tests/test_file.py ...]
# Env:    TSAN_GOLD_TIMEOUT     per-file wall-clock budget in seconds (default 900)
#         TSAN_GOLD_EXTENDED=1  add the slow files (test_swarm_mn_sched, ~5-8 min under TSan)
# Exit:   0  every file ran normally, no NEW reports
#         1  NEW reports (untriaged races)
#         2  the lane is broken or a file did not run normally: build or teeth
#            failure; a file that crashed, timed out, was not collected, or failed
#            a test outside tools/verify/tsan_gold_expected_fail.txt; a TSan
#            FATAL / CHECK failure in any log; or a TSan deadly-signal report
#            (SEGV, stack-overflow, ...) that is not triaged KNOWN.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${STACKWEAVE_TSAN_PYTHON:-}"
[ -n "$PY" ] && [ -x "$PY" ] || { echo "set STACKWEAVE_TSAN_PYTHON to a --with-thread-sanitizer python (tools/build_tsan_cpython.sh)"; exit 2; }
"$PY" -c 'import sysconfig,sys; a=sysconfig.get_config_var("CONFIG_ARGS") or ""; sys.exit(0 if "--with-thread-sanitizer" in a and sysconfig.get_config_var("Py_GIL_DISABLED") else 1)' \
    || { echo "$PY is not a free-threaded --with-thread-sanitizer build"; exit 2; }
KNOWN="$ROOT/tools/verify/tsan_gold_known.txt"
EXPECTED_FAIL="$ROOT/tools/verify/tsan_gold_expected_fail.txt"
TIMEOUT="${TSAN_GOLD_TIMEOUT:-900}"
SA=""
if [ "$(uname -s)" = Linux ]; then
    command -v setarch >/dev/null 2>&1 && SA="setarch $(uname -m) -R" \
        || echo "WARNING: setarch not found; TSan aborts under ASLR on Linux 6.x"
fi

LOGDIR="$(mktemp -d "${TMPDIR:-/tmp}/sw_tsan_gold.XXXXXX")"
SUPP="$LOGDIR/suppressions.txt"
: > "$SUPP"
if [ -n "${STACKWEAVE_TSAN_CPYTHON_SUPP:-}" ] && [ -f "$STACKWEAVE_TSAN_CPYTHON_SUPP" ]; then
    cat "$STACKWEAVE_TSAN_CPYTHON_SUPP" >> "$SUPP"
else
    echo "WARNING: STACKWEAVE_TSAN_CPYTHON_SUPP unset -- CPython's known free-threading races will show as NEW"
fi

echo "================ stackweave GOLD TSan lane ================"
echo "  python : $PY"
echo "  logs   : $LOGDIR"

TREE="$LOGDIR/tree"
mkdir -p "$TREE"
( cd "$ROOT" && git ls-files -z --cached --others --exclude-standard \
      | while IFS= read -r -d '' f; do [ -e "$f" ] && printf '%s\0' "$f"; done \
      | tar --null -T - -cf - ) | tar -xf - -C "$TREE" \
    || { echo "  could not copy the working tree to $TREE"; exit 2; }
cd "$TREE"

echo "-- building the instrumented extension (STACKWEAVE_TSAN=1, in $TREE) --"
if ! $SA env STACKWEAVE_TSAN=1 \
        STACKWEAVE_EXTRA_CFLAGS="-DPy_TSTATE_ALLOC_HOME -DPy_TSTATE_EXEC_HOME ${STACKWEAVE_EXTRA_CFLAGS:-}" \
        "$PY" setup.py build_ext --inplace --force > "$LOGDIR/build.log" 2>&1; then
    echo "  BUILD FAILED -- $LOGDIR/build.log"; tail -20 "$LOGDIR/build.log"; exit 2
fi
SO="$(ls src/stackweave_c*.so 2>/dev/null | head -1)"
# An instrumented object calls __tsan_func_entry (nm prints it with a leading
# underscore on macOS); checking the dynamic deps instead misses a clang-built
# Linux ext, which links the runtime statically into the interpreter.
[ -n "$SO" ] && nm -u "$SO" 2>/dev/null | grep -q '__tsan_func_entry' \
    || { echo "  ${SO:-src/stackweave_c*.so} is not TSan-instrumented"; exit 2; }

# exitcode=0: a test judges its own result; races are read from the logs.  It
#   also makes TSan's own fatal reports exit 0, so the logs are checked for them.
# history_size=7: the maximum; smaller values leave the OTHER access of a hub-
#   thread race with a one-frame, unsymbolised stack.
# abort_on_error=0: macOS defaults it on, which turns the exit-time report into
#   SIGABRT (and a slow crash report) for every process that saw a race.
tsan_opts() {
    echo "halt_on_error=0:abort_on_error=0:exitcode=0:report_signal_unsafe=0:history_size=7:suppressions=$SUPP:log_path=$1/tsan"
}
broken=""      # newline-separated "label: why" for files that did not run normally
note_broken() { broken="${broken}    $1"$'\n'; }
LAST_RC=0
run() {  # label, cmd...
    local label="$1"; shift
    local d="$LOGDIR/$label"; mkdir -p "$d"
    local t0; t0=$(date +%s)
    $SA env PYTHON_GIL=0 PYTHONPATH="$TREE/src" TSAN_OPTIONS="$(tsan_opts "$d")" \
        perl -e 'alarm shift; exec @ARGV' "$TIMEOUT" "$@" > "$d/out.txt" 2>&1
    LAST_RC=$?
    local n; n=$(cat "$d"/tsan.* 2>/dev/null | grep -c '^WARNING: ThreadSanitizer')
    printf -- "-- %-34s rc=%-3s %4ss  reports=%s  | %s\n" "$label" "$LAST_RC" "$(( $(date +%s) - t0 ))" "$n" \
        "$(grep -v '^\s*$' "$d/out.txt" | tail -1 | cut -c1-70)"
    # "ThreadSanitizer: ERROR:" is TSan refusing its options (the run went
    # uninstrumented-silent and exited 0).
    if cat "$d"/tsan.* "$d/out.txt" 2>/dev/null \
            | grep -qE 'FATAL: ThreadSanitizer|CHECK failed|ThreadSanitizer: ERROR:'; then
        note_broken "$label: TSan FATAL / CHECK failure / option error (see $d)"
    fi
}

echo "-- teeth (tools/verify/tsan_teeth.py) --"
run teeth_race  "$PY" tools/verify/tsan_teeth.py race
race_rc=$LAST_RC
run teeth_clean "$PY" tools/verify/tsan_teeth.py clean
clean_rc=$LAST_RC
teeth_ok=1
[ "$race_rc" = 0 ] && [ "$clean_rc" = 0 ] || { echo "  teeth: a teeth script exited $race_rc / $clean_rc"; teeth_ok=0; }
grep -q "cross-hub" "$LOGDIR/teeth_race/out.txt" || { echo "  teeth: race writers never ran on two hubs"; teeth_ok=0; }
grep -q "cross-hub" "$LOGDIR/teeth_clean/out.txt" \
    || { echo "  teeth: clean writer never migrated, so the control proves nothing about migration"; teeth_ok=0; }
cat "$LOGDIR"/teeth_race/tsan.* 2>/dev/null | grep -q "^SUMMARY: ThreadSanitizer: data race .* in pack_single" \
    || { echo "  teeth: planted race (memoryobject.c pack_single) NOT reported (no instrumentation, or merged fiber histories)"; teeth_ok=0; }
if cat "$LOGDIR"/teeth_clean/tsan.* 2>/dev/null | grep -q "SUMMARY: ThreadSanitizer"; then
    echo "  teeth: clean control reported a race (lost happens-before across park/migration)"; teeth_ok=0
fi
[ -z "$broken" ] || { printf "%s" "$broken"; teeth_ok=0; }
[ "$teeth_ok" = 1 ] || { echo "  LANE BROKEN -- see $LOGDIR/teeth_*"; exit 2; }
echo "  teeth OK: planted race reported, clean control silent, both cross-hub"

if [ $# -gt 0 ]; then
    FILES="$*"
else
    FILES="tests/test_mn.py tests/test_local_wake.py tests/test_cross_hub_migration.py
           tests/test_tlbc_parked_frame_gc.py tests/test_fiber_tstate_isolation.py
           tests/test_chan.py tests/test_chan_stress.py tests/test_sync.py
           tests/test_sync_primitives.py tests/test_mn_park.py tests/test_mn_teardown.py"
    [ "${TSAN_GOLD_EXTENDED:-0}" = 1 ] && FILES="$FILES tests/test_swarm_mn_sched.py"
fi

# Is a failing pytest node id on the expected-failure list?  Entries are node-id
# prefixes ("file::test", which also covers parametrised "[...]" ids).
expected_fail() {
    awk -F' *[|] *' -v id="$1" '
        /^[[:space:]]*(#|$)/ { next }
        { k = $1; sub(/[[:space:]]+$/, "", k)
          if (substr(id, 1, length(k)) == k) { found = 1; exit } }
        END { exit !found }' "$EXPECTED_FAIL" 2>/dev/null
}

echo "-- test files under TSan (one process each, ${TIMEOUT}s budget) --"
for f in $FILES; do
    # The label names a log directory that goes into TSAN_OPTIONS' log_path, where
    # ':' is the option separator -- so no "::" from a pytest node id.
    label="$(basename "$f" | sed -E 's/\.py(::|$)/\1/; s/[^A-Za-z0-9_.-]+/_/g')"
    run "$label" "$PY" -m pytest "$f" -q -p no:cacheprovider --no-header
    out="$LOGDIR/$label/out.txt"
    case "$LAST_RC" in
        0)  grep -qE '[0-9]+ passed' "$out" \
                || note_broken "$label: rc=0 but no pytest result line (see $out)" ;;
        1)  # Test failures: fine only if every one is an expected TSan-environment failure.
            ids="$(sed -nE 's/^(FAILED|ERROR) ([^ ]+).*/\2/p' "$out")"
            [ -n "$ids" ] || note_broken "$label: rc=1 but no FAILED/ERROR lines (see $out)"
            for id in $ids; do
                if expected_fail "$id"; then echo "     expected under TSan: $id"
                else note_broken "$label: unexpected failure $id"; fi
            done ;;
        4|5) note_broken "$label: rc=$LAST_RC -- pytest usage error or nothing collected (does $f exist?)" ;;
        142) note_broken "$label: rc=142 -- hit the ${TIMEOUT}s alarm (hang?)" ;;
        *)  if [ "$LAST_RC" -ge 128 ]; then note_broken "$label: rc=$LAST_RC -- killed by signal $((LAST_RC - 128))"
            else note_broken "$label: rc=$LAST_RC -- pytest interrupted or internal error"; fi ;;
    esac
done

echo "----------------------------------------------------------------"
echo "  race reports by SUMMARY site (KNOWN = triaged in $(basename "$KNOWN")):"
new=0
# The teeth's planted race is not a finding: summarise the test files only.
summaries="$(ls -d "$LOGDIR"/*/ | grep -v -e '/teeth_' -e '/tree/' | while read -r d; do cat "$d"tsan.* 2>/dev/null; done \
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
        elif echo "$line" | grep -qE '^ *[0-9]+ (SEGV|BUS|ILL|FPE|ABRT|stack-overflow)'; then
            # TSan's own deadly-signal report: with exitcode=0 the process exits
            # 0, so this line is the only trace of the crash.
            printf "    CRASH        %s\n" "$line"; note_broken "TSan deadly-signal report: ${line#"${line%%[![:space:]]*}"}"
        else printf "    NEW          %s\n" "$line"; new=1; fi
    done <<< "$summaries"
fi
if [ -n "$broken" ]; then
    echo "  files that did NOT run normally:"
    printf "%s" "$broken"
fi
echo "  logs: $LOGDIR   (instrumented tree: $TREE)"
echo "================================================================"
[ -z "$broken" ] || exit 2
exit "$new"

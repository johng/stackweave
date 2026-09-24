#!/usr/bin/env bash
# rr_chaos.sh -- hunt lost wakes under rr's chaos scheduler (duty-cycle stage).
#
# `rr record --chaos` deliberately randomizes scheduling to surface the
# pathological interleavings that lose wakes -- and any hang it finds comes with
# a DETERMINISTICALLY REPLAYABLE recording (rr replay), ending the
# one-in-a-thousand-repro problem for this bug class.  Subjects are lifefuzz
# programs: generative, ALWAYS-terminating, self-checking -- so a timeout under
# chaos is a real lost wake and a nonzero exit is a real bug, never flake.
#
# Availability gate: rr needs working hardware perf counters.  This VMware
# guest's vPMU rejects sample_period < 32, and stock rr's self-check uses
# period=1, so stock rr aborts -- SOLVED by the min-period clamp in
# tools/rr_vpmu_min_period.patch (build it with tools/build_patched_rr.sh).
# The host-side route this comment used to recommend (vpmu.enable=TRUE / full
# PMU passthrough) was never needed.  Full rationale + validation in
# docs/dev/rr_vpmu_status.md.
#
# The gate stays regardless: a machine without the patched rr build skips
# cleanly and auto-activates the day `rr record /bin/true` works.  Same
# pattern as hang_hunter's rr_capture.
#
# Usage:  rr_chaos.sh <duration_s> <artifact_dir>
#   Loops lifefuzz seeds under rr chaos for duration_s.  Clean run -> trace
#   deleted.  Timeout/crash/nonzero -> trace + log moved to artifact_dir and a
#   line is written for the caller (duty_cycle.sh inboxes it).
set +e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${STACKWEAVE_PYTHON:-$HOME/.pyenv/versions/3.14.4t/bin/python3}"
DUR="${1:?usage: rr_chaos.sh <duration_s> <artifact_dir>}"
ART="${2:?usage: rr_chaos.sh <duration_s> <artifact_dir>}"
PER_RUN_TIMEOUT="${RR_CHAOS_TIMEOUT:-60}"   # generous: rr overhead + chaos delays
cd "$ROOT"
mkdir -p "$ART"

# --- availability gate -------------------------------------------------------
if ! command -v rr >/dev/null 2>&1; then
  echo "rr-chaos SKIP: rr not installed"; exit 0
fi
GATE_DIR="$(mktemp -d)"
if ! _RR_TRACE_DIR="$GATE_DIR" rr record /bin/true >/dev/null 2>&1; then
  rm -rf "$GATE_DIR"
  echo "rr-chaos SKIP: rr cannot record on this host (vPMU -- see docs/dev/rr_vpmu_status.md)"
  exit 0
fi
rm -rf "$GATE_DIR"

TRACES="$ART/rr_traces"; mkdir -p "$TRACES"
end=$(( $(date +%s) + DUR )); n=0; findings=0
echo "rr-chaos: ${DUR}s of lifefuzz seeds under 'rr record --chaos'"
while [ "$(date +%s)" -lt "$end" ]; do
  seed=$(( (n * 1103515245 + 12345) % 2000000000 )); n=$((n+1))
  log="$ART/seed_${seed}.log"
  _RR_TRACE_DIR="$TRACES" timeout -k 5 "$PER_RUN_TIMEOUT" \
      rr record --chaos env PYTHON_GIL=0 PYTHONPATH="$ROOT/src" \
      "$PY" tools/lifefuzz/lifefuzz.py run "$seed" --timeout 30 \
      >"$log" 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    rm -f "$log"
    # keep the trace dir bounded: drop the newest (clean) recording
    latest="$(ls -1dt "$TRACES"/*/ 2>/dev/null | head -1)"
    [ -n "$latest" ] && rm -rf "$latest"
  else
    findings=$((findings+1))
    kind="rr-chaos-fail"; [ "$rc" -ge 124 ] && kind="rr-chaos-HANG"
    latest="$(ls -1dt "$TRACES"/*/ 2>/dev/null | head -1)"
    keep="$ART/${kind}_seed${seed}"
    [ -n "$latest" ] && mv "$latest" "$keep" 2>/dev/null
    echo "FINDING $kind seed=$seed rc=$rc log=$log trace=$keep (replay: rr replay $keep)"
  fi
done
rmdir "$TRACES" 2>/dev/null
echo "rr-chaos: $n runs, $findings findings"
exit 0

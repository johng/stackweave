#!/usr/bin/env bash
# linz_hunt_forever.sh -- generative LINEARIZABILITY fault hunt, forever.
#
# Loops the linearizability battery (tools/lincheck/linz/battery.py) over
# ever-advancing seed ranges across every primitive (chan/mutex/rwmutex/
# semaphore/waitgroup/event).  Each run records a real concurrent history on
# the M:N scheduler and checks it against the sequential reference spec with
# the pure-Python WGL checker.  Any NOT-LINEARIZABLE verdict is a genuine
# correctness bug in the primitive; its workload replays from ONE integer:
#   python tools/lincheck/linz/battery.py <primitive> --seeds S S+1 -v
# The battery records real-time (--wallclock) histories by default, so the
# schedule itself does not replay; --seeded pins it, but mn_init refuses a
# seeded run until the seeded M:N scheduler is re-implemented for migration.
#
# Niced to 19 (alongside the rr fleet / simfd hunt) so it never starves
# big100/cserve.  Log: ${STACKWEAVE_SOAK_DIR:-$HOME/runloom-soak}/linz_hunt/.
set +e
cd "$(dirname "$0")/../.." || exit 9
DIR="${STACKWEAVE_SOAK_DIR:-$HOME/runloom-soak}/linz_hunt"
mkdir -p "$DIR"
SUMMARY="$DIR/SUMMARY.txt"
PY="${PYTHON:-$HOME/.pyenv/versions/3.14.4t/bin/python3}"
export STACKWEAVE_PYTHON="$PY"
N="${HUNT_BATCH:-40}"                 # seeds per primitive per round
seed0="${HUNT_SEED0:-0}"
round=0
echo "[$(date -u +%FT%TZ)] linz hunt START seed0=$seed0 batch=$N" >> "$SUMMARY"
while true; do
  round=$((round + 1))
  for prim in chan mutex rwmutex semaphore waitgroup event; do
    log="$DIR/${prim}_round${round}_seed${seed0}.log"
    nice -n 19 "$PY" tools/lincheck/linz/battery.py "$prim" \
        --seeds "$seed0" "$((seed0 + N))" > "$log" 2>&1
    rc=$?
    done_line=$(grep -E "== battery:" "$log" | tail -1)
    ts=$(date -u +%FT%TZ)
    if [ "$rc" != "0" ]; then
      echo "[$ts] *** FINDING *** prim=$prim seeds [$seed0,$((seed0+N))) rc=$rc -> ${done_line:-crash}  (log: $log)" >> "$SUMMARY"
    else
      echo "[$ts] clean prim=$prim seeds [$seed0,$((seed0+N))) -> ${done_line:-no-summary}" >> "$SUMMARY"
      rm -f "$log"                    # keep only finding logs
    fi
  done
  seed0=$((seed0 + N))
done

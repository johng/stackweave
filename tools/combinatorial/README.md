# tools/combinatorial — config-matrix interaction testing

stackweave's runtime knobs multiply: steal randomization × ready-ring fairness
× idle backoff × sysmon × … . Bugs hide in the *interactions*, not in any single
setting, but the full cartesian product is wasteful to test and
one-factor-at-a-time misses interactions entirely.

`covering.py` builds a **t-way covering array** — a small set of configurations
in which every combination of `t` knob-values appears at least once — and runs
the M:N scheduler fuzzer (`mn_stress`) under each. Empirically most interaction
faults are triggered by ≤2–3 factors, so pairwise/3-way catches them cheaply.

> Kuhn, Wallace, Gallo, *Software Fault Interactions and Implications for
> Software Testing*, IEEE TSE 2004. Cohen et al, AETG (greedy construction).

## Run it

```sh
PY=~/.pyenv/versions/3.14.4t/bin/python3
$PY tools/combinatorial/covering.py --list            # array + coverage stats
$PY tools/combinatorial/covering.py --iters 40        # run each config
$PY tools/combinatorial/covering.py --t 3             # 3-way (stronger, more rows)
```

Also `scripts/check_all.sh combo`.

## Factors

The matrix covers the scheduler knobs that still change M:N behaviour:
`STACKWEAVE_SCHED_RANDOM` (randomized steal victim and spawn placement),
`STACKWEAVE_READY_STARVE_BOUND` (`0` = ready ring always first, `64` = default
fairness turn), `STACKWEAVE_IDLE_BACKOFF_MS` (`1` = no idle backoff, `32` =
default) and `STACKWEAVE_SYSMON` (logging only; sysmon always runs). The
cartesian config space is reduced to a small pairwise covering array.

Cross-hub migration, preemption and the netpoll backend used to be factors
here too. They are no longer switchable, so they left the matrix. The tool's
first success story came from that era: the very first pairwise run with the
then-experimental woken-stealing knob added immediately isolated a
single-factor SIGSEGV — every failing config had it on, every passing one had
it off, pointing straight at the live-frame-migration crash of the time.

## Candidate for `all`

`combo` is currently opt-in (like `bench`). Once the matrix has run cleanly a
few times on the target hardware it's a good candidate to fold into
`check_all.sh all`.

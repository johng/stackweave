# stackweave security verification

Gap-filling security checks beyond the existing verification stack
(TSan/ASan/UBSan, lincheck, dst, mutation, formal methods, fault injection).
Free-threaded 3.13t. See **FINDINGS.md** for the results and rationale.

## Run

```sh
tools/security/run_all.sh          # S1-S4 (builds the C helper, runs valgrind)
```

| file | check |
| --- | --- |
| `test_stack_scrub.py` + `stack_scrub_helper.c` | S1: recycled-stack data hygiene; verifies `set_stack_scrub(True)` prevents cross-goroutine stack leakage |
| `test_signal_storm.py` | S2: scheduler under a 1 kHz signal storm |
| `test_refcount_race.py` | S3: cross-hub shared-object refcount stability |
| `vg_smoke.py` + `stackweave.supp` | S4: valgrind memcheck workload + by-design suppressions |

## Headline finding (S1)

Recycled goroutine stacks were not scrubbed -- a new goroutine could read the
previous one's stack (TLS keys / request bodies; the aio bridge runs OpenSSL
on these). Fixed with an **opt-in** scrub (`set_stack_scrub(True)`, or
`stackweave.optimize("secure")`). It was first `MADV_DONTNEED`-based (flat
~+8.8 us/goroutine regardless of stack size); it now wipes only the resident
pages (mincore + memset) on Linux, falling back to `MADV_DONTNEED` when
mincore can't be used. Default off (spawn-heavy code pays nothing); the aio
bridge should enable it. Full detail in FINDINGS.md.

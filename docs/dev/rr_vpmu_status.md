# rr under a VMware vPMU

**Status: WORKING**, via a two-line patch to rr. No host-side vmx change was
needed in the end.

Six files across this tree point at this document -- the patch, the build
script, the probe, `tools/README.md`, and two soak stages. It did not exist
until 2026-08-21; the rationale lived only in the patch comment and a script
header. This is that document.

## The problem

rr replays by counting **retired conditional branches** and firing a
performance-monitoring interrupt at an exact count. That needs a hardware
counter supporting (a) sampling and (b) `PERF_EVENT_IOC_PERIOD` down to a very
small period -- rr's own self-check, `check_for_ioc_period_bug()`, sets
`period = 1`.

VMware's vPMU historically virtualizes *counting* only. This host has a partial
configuration: counting works, sampling works, `IOC_PERIOD` works for large
periods -- but small periods are rejected with `EINVAL`. rr's self-check hits
that and calls `FATAL()`, so **stock rr aborts before recording anything**.

Measure it in one command:

```console
$ cc -O2 -o /tmp/rr_vpmu_probe tools/rr_vpmu_probe.c && /tmp/rr_vpmu_probe
OK: sampling retired-branch counter opens.
vPMU REJECTS small sample_period: min accepted = 32 (period 31 = EINVAL).
==> rr's check_for_ioc_period_bug uses period=1 -> it ABORTS here.
```

The minimum is exactly **32**. That is an interrupt-rate sanity guard on the
hypervisor side, not a precision limit -- which is the whole reason a clamp is
safe.

## The fix

`tools/rr_vpmu_min_period.patch`, two hunks in `src/PerfCounters.cc`:

1. `check_for_ioc_period_bug()` probes with `period = 32` instead of `1`, so the
   self-check runs here instead of aborting.
2. `PerfCounters::reset()` clamps any `ticks_period` below 32 up to 32.

### Why the clamp does not break replay

This is the part worth understanding before trusting it.

The **retired-branch count itself is exactly reproducible on this vPMU** --
verified, delta = 0 across 12 runs. What the hypervisor refuses is a tiny
*interrupt period*, not accurate counting. So clamping means rr fires its PMI a
few branches later than it asked for, and then **single-steps the remainder** to
land on the exact target.

That is not a new mechanism: rr already single-steps through the PMI *skid*
(interrupts are never precisely delivered on real hardware either), and a
sub-32-branch overshoot is well inside the skid it tolerates. Replay stays
exact.

If the branch count were *not* reproducible here, this would be unsound and no
clamp could rescue it -- so re-verify reproducibility before trusting the patch
on a different host.

### Validated

Record + replay with **zero divergence** on:

- single-threaded programs
- multi-threaded with contended locks
- a stackweave goroutine workload on the epoll backend

## Building it

```bash
tools/build_patched_rr.sh          # RR_SRC=~/projects/rr-src by default
```

Clones rr **5.7.0**, applies the patch, builds with `-Ddisable32bit=ON` (avoids
needing multilib), and installs to `/usr/local/bin/rr`, which **shadows the
distribution's unpatched rr** on PATH. Verify with `rr --version` and:

```bash
rr record /bin/true      # the gate every rr-dependent stage uses
```

## What depends on this

These stages gate on `rr record /bin/true` succeeding and **skip cleanly** when
it does not, so an unpatched machine degrades rather than fails:

| stage | what it does |
| --- | --- |
| `tools/soak/rr_chaos.sh` | lifefuzz seeds under `rr record --chaos`, hunting lost wakes |
| `tools/soak/rr_fleet.sh` | fleet of rr recordings across seeds |
| `tools/hang_hunter/rr_capture.py` | captures a replayable trace when a hang is caught |

The gate is the right design: the day rr stops recording (kernel change, host
vPMU change, a machine without the patched build) those stages go quiet instead
of producing noise.

## A stale comment, now corrected

`rr_chaos.sh` used to say the vPMU "currently rejects rr's counter setup" and
"needs a host-side vmx change: `vpmu.enable=TRUE` / full PMU passthrough". That
was true *before* the patch existed, and the host-side route was never taken --
the clamp was. The comment has been updated; if you find that phrasing elsewhere
it predates this.

## Re-checking after any host or kernel change

```bash
cc -O2 -o /tmp/rr_vpmu_probe tools/rr_vpmu_probe.c && /tmp/rr_vpmu_probe
rr record /bin/true && echo "records fine"
```

If the probe ever reports a minimum period **above 32**, the clamp constant in
the patch must move with it -- it is hard-coded in both hunks.

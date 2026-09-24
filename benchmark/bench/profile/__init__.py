"""Profiling drivers for the stackweave perf campaign.

These don't time anything themselves -- the external tool (perf, bpftrace,
cProfile, strace, valgrind) is the instrument.  run_workload.py provides a
single stackweave workload as a clean process to attach to.
"""

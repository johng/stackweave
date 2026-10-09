# benchmark/

Everything lives in [`bench/`](bench/README.md): one set of workloads run on
stackweave (per feature config) and on threads, asyncio, uvloop, trio, gevent
and Go, compared in one table.

    PYTHONPATH=src:benchmark PYTHON_GIL=0 python -m bench.compare    # or scripts/bench.sh

Committed runs, newest last: [`bench/results/compare/`](bench/results/compare/).

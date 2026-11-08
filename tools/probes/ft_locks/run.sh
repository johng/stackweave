#!/bin/bash
# usage: run.sh probe.py [args]  -- runs with the worktree's build
P=$(cd "$(dirname "$0")" && pwd)
W=$(cd "$P/../../.." && pwd)
PY=${PY:-$HOME/.pyenv/versions/3.14.4t-mig/bin/python3.14t}
cd "$P"
PYTHON_GIL=0 PYTHONPATH="$W/src:$P" exec timeout ${TMO:-60} "$PY" -X faulthandler "$@"

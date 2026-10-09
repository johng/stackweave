"""Runtime comparison: stackweave (per feature config) vs threads, asyncio,
uvloop, trio, gevent and Go, on the same workloads, in one table.

The suites are bench.mnsched (scheduler: park/wake, spawn, yield, sync
primitives, scaling, latency), bench.echo (in-process TCP echo), bench.apps
(application-shaped programs: an HTTP JSON API, an API gateway, a parse+hash
pipeline, pub/sub, a crawler -- defined once in bench/appspec.py) and
bench.memory (RSS per parked unit).  bench.baselines (+ baselines_apps) runs
them on the other Python runtimes and gobench/ on Go, under the same entry
names and inner counts, so every row lines up.

Every column runs in its own process (stackweave's switches are read from the
environment when the hubs start), in interleaved passes (A B C, then C B A,
...) because run-to-run drift on one box is larger than most real deltas.
Each cell is the median over passes (throughput: higher is better; latency
and RSS/unit: lower is better); the bracket compares it with stackweave
`default`: a percentage for a stackweave config, a ratio for another runtime,
marked ▲ (better than stackweave default) / ▼ (worse) only when every pass
agrees and the gap is over 3%.

    PYTHONPATH=src:benchmark PYTHON_GIL=0 python -m bench.compare
    ... --configs default,stack-arena --runtimes asyncio,go --passes 2
    ... --baseline-python /path/to/stock/python3.14t   # threads + event loops
    ... --gil-python /path/to/python3.14 --runtimes asyncio-gil,uvloop-gil
    ... --build O2=src --build O3=/path/to/other/tree/src   # A/B two builds
    ... --suite-args mnsched="--no-scaling" --quick

Runtimes this box cannot run (io_uring configs off Linux, an event loop not
installed in the baseline interpreter, no `go`) are skipped with a note.
Results: one JSON + log per (suite, column, pass) and summary.md /
summary.json in --out-dir (default results/compare/<stamp>).
"""
import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH_ROOT = os.path.dirname(HERE)                  # benchmark/
REPO = os.path.dirname(BENCH_ROOT)

# stackweave config -> (env overrides, needs io_uring)
CONFIGS = {
    "default": ({}, False),
    "stack-arena": ({"STACKWEAVE_STACK_ARENA": "1"}, False),
    "optimize-throughput": ({"STACKWEAVE_BENCH_OPTIMIZE": "throughput"}, False),
    "tcpconn-iouring": ({"STACKWEAVE_TCPCONN_IOURING": "1"}, True),
    "iouring-loop": ({"STACKWEAVE_IOURING_LOOP": "1", "STACKWEAVE_IOURING_MS": "0"}, True),
    "iouring-loop-ms": ({"STACKWEAVE_IOURING_LOOP": "1", "STACKWEAVE_IOURING_MS": "1",
                         "STACKWEAVE_IOURING_MS_BUFS": "1024"}, True),
}
# other runtime -> (bench.baselines kind or "go", interpreter, module to probe).
# The "-gil" variants run on --gil-python, a GIL build: a single-threaded
# loop's best case.
RUNTIMES = {
    "threads": ("threads", "baseline", None),
    "asyncio": ("asyncio", "baseline", None),
    "uvloop": ("uvloop", "baseline", "uvloop"),
    "trio": ("trio", "baseline", "trio"),
    "gevent": ("gevent", "baseline", "gevent"),
    "asyncio-gil": ("asyncio", "gil", None),
    "uvloop-gil": ("uvloop", "gil", "uvloop"),
    "go": ("go", None, None),
}
DEFAULT_CONFIGS = list(CONFIGS)
DEFAULT_RUNTIMES = ["threads", "asyncio", "uvloop", "trio", "gevent", "go"]
SUITES = ("mnsched", "echo", "apps", "memory")
RT = "rt"            # the "build" slot of a non-stackweave column's key


def is_free_threaded(py):
    code = "import sysconfig; print(int(bool(sysconfig.get_config_var('Py_GIL_DISABLED'))))"
    try:
        return subprocess.run([py, "-c", code], capture_output=True, text=True,
                              timeout=60).stdout.strip() == "1"
    except Exception:
        return False


def py_env(py, pythonpath):
    env = dict(os.environ)
    # Start from a clean feature slate: an inherited switch would leak into
    # every config, including the "default" it is compared against.
    for k in list(env):
        if k.startswith("STACKWEAVE_") and k not in ("STACKWEAVE_BENCH_HUBS",):
            del env[k]
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    if is_free_threaded(py):
        env["PYTHON_GIL"] = "0"
    else:
        env.pop("PYTHON_GIL", None)      # a GIL build refuses PYTHON_GIL=0
    return env


def probe(py, env, code):
    try:
        return subprocess.run([py, "-c", code], env=env, capture_output=True,
                              text=True, timeout=60).returncode == 0
    except Exception:
        return False


def run_cmd(cmd, env, out_path, timeout):
    t0 = time.monotonic()
    p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout,
                       cwd=REPO)
    dt = time.monotonic() - t0
    log = out_path[:-5] + ".log"
    with open(log, "w") as f:
        f.write(p.stdout)
        f.write(p.stderr)
    if p.returncode != 0 or not os.path.exists(out_path):
        tail = "\n".join((p.stdout + p.stderr).splitlines()[-15:])
        raise RuntimeError("%s failed (rc=%s, %.0fs); log %s\n%s"
                           % (" ".join(cmd), p.returncode, dt, log, tail))
    return dt


def load(path):
    with open(path) as f:
        return json.load(f)


def summarize(runs):
    """runs: {(suite, key): [doc, doc, ...]} one doc per pass.
    Returns per suite: (kind, name) -> {key: [value per pass]}"""
    table = {}
    for (suite, key), docs in runs.items():
        t = table.setdefault(suite, {})
        for d in docs:
            for r in d.get("results") or []:
                t.setdefault(("tput", r["name"]), {}).setdefault(key, []).append(r["ops_per_s"])
            for r in d.get("latency") or []:
                t.setdefault(("p50", r["name"]), {}).setdefault(key, []).append(r["p50_us"])
                t.setdefault(("p99", r["name"]), {}).setdefault(key, []).append(r["p99_us"])
            for r in d.get("memory") or []:
                t.setdefault(("mem", r["name"]), {}).setdefault(key, []).append(r["bytes_per_unit"])
    return table


def fmt_row(kind, name, per_key, keys, base_key):
    base = per_key.get(base_key)
    cells = []
    for k in keys:
        vals = per_key.get(k)
        if not vals:
            cells.append("-")
            continue
        med = statistics.median(vals)
        if kind == "tput":
            cell = "%.0f" % med if med >= 100 else "%.1f" % med
        elif kind == "mem":
            cell = "%.1f KB" % (med / 1024)
        else:
            cell = "%.1f us" % med
        if base and k != base_key and len(vals) == len(base):
            # per-pass ratios; a gap counts only if every pass agrees on side
            rs = [v / b for v, b in zip(vals, base) if b]
            if rs:
                r = statistics.median(rs)
                agree = len(rs) >= 2 and (all(x > 1 for x in rs) or all(x < 1 for x in rs))
                better = (r > 1) if kind == "tput" else (r < 1)
                mark = ("" if not agree or abs(r - 1) < 0.03 else (" ▲" if better else " ▼"))
                if k[0] == RT:
                    cell += " (%s×%s)" % (("%.2f" if r < 10 else "%.0f") % r, mark)
                else:
                    cell += " (%+.1f%%%s)" % ((r - 1) * 100, mark)
        cells.append(cell)
    label = {"tput": name, "mem": "%s [RSS/unit]" % name}.get(kind, "%s [%s]" % (name, kind))
    return "| %s | %s |" % (label, " | ".join(cells))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--suites", default=",".join(SUITES))
    ap.add_argument("--configs", default=",".join(DEFAULT_CONFIGS),
                    help="stackweave feature configs (the first is the baseline)")
    ap.add_argument("--runtimes", default=",".join(DEFAULT_RUNTIMES),
                    help="other runtimes: %s ('' = none)" % ", ".join(RUNTIMES))
    ap.add_argument("--baseline-python",
                    default=os.environ.get("STACKWEAVE_BASELINE_PYTHON", sys.executable),
                    help="interpreter for threads and the event loops: a stock "
                         "free-threaded build with uvloop/trio/gevent installed "
                         "(env STACKWEAVE_BASELINE_PYTHON)")
    ap.add_argument("--gil-python", default=os.environ.get("STACKWEAVE_GIL_PYTHON"),
                    help="a GIL build, for the -gil runtimes (env STACKWEAVE_GIL_PYTHON)")
    ap.add_argument("--build", action="append", default=[],
                    help="NAME=SRC_DIR: an extension build to run (repeatable); "
                         "default one build, 'cur' = this tree's src/")
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--suite-args", action="append", default=[],
                    help='SUITE="args" passed through to that stackweave suite')
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--from-dir", default=None,
                    help="re-summarize the runs already in DIR (no new runs); "
                         "only passes complete for every column are used")
    ap.add_argument("--note", action="append", default=[],
                    help="a caveat line for the summary header (repeatable)")
    args = ap.parse_args(argv)
    if args.from_dir:
        return summarize_dir(args)

    py = sys.executable
    suites = [x for x in args.suites.split(",") if x]
    builds = []
    for b in args.build or ["cur=" + os.path.join(REPO, "src")]:
        name, _, src = b.partition("=")
        builds.append((name, os.path.abspath(src)))
    suite_args = {}
    for sa in args.suite_args:
        k, _, v = sa.partition("=")
        suite_args[k] = v.split()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.abspath(args.out_dir or os.path.join(HERE, "results", "compare", stamp))
    os.makedirs(out_dir, exist_ok=True)

    have_uring = probe(py, py_env(py, [builds[0][1]]),
                       "import stackweave_c, sys; "
                       "sys.exit(0 if stackweave_c.iouring_available() else 1)")
    configs = []
    for c in [x for x in args.configs.split(",") if x]:
        if c not in CONFIGS:
            ap.error("unknown config %r (have: %s)" % (c, ", ".join(CONFIGS)))
        if CONFIGS[c][1] and not have_uring:
            print("skip %-20s io_uring not available on this box" % c)
            continue
        configs.append(c)

    interp = {"baseline": args.baseline_python, "gil": args.gil_python}
    runtimes, gobin = [], None
    for r in [x for x in args.runtimes.split(",") if x]:
        if r not in RUNTIMES:
            ap.error("unknown runtime %r (have: %s)" % (r, ", ".join(RUNTIMES)))
        kind, which, mod = RUNTIMES[r]
        if kind == "go":
            if not shutil.which("go"):
                print("skip %-20s no `go` on PATH" % r)
                continue
            gobin = os.path.join(out_dir, "gobench")
            subprocess.run(["go", "build", "-o", gobin, "."], check=True,
                           cwd=os.path.join(HERE, "gobench"))
        else:
            rpy = interp[which]
            if not rpy:
                print("skip %-20s no --gil-python given" % r)
                continue
            if not probe(rpy, py_env(rpy, [BENCH_ROOT]), "import %s" % (mod or "asyncio")):
                print("skip %-20s %s not importable in %s" % (r, mod, rpy))
                continue
        runtimes.append(r)

    keys = [(b, c) for b, _ in builds for c in configs] + [(RT, r) for r in runtimes]
    key_name = {k: (k[1] if len(builds) == 1 or k[0] == RT else "%s/%s" % k) for k in keys}
    base_key = keys[0]
    print("builds: %s\nconfigs: %s\nruntimes: %s\nsuites: %s  passes: %d  out: %s\n"
          % (", ".join("%s=%s" % b for b in builds), ", ".join(configs),
             ", ".join(runtimes) or "-", ", ".join(suites), args.passes, out_dir))

    def command(suite, key, out):
        extra = ["--quick"] if args.quick else []
        if key[0] != RT:
            env = py_env(py, [dict(builds)[key[0]], BENCH_ROOT])
            env.update(CONFIGS[key[1]][0])
            return ([py, "-X", "gil=0", "-m", "bench." + suite, "--out", out]
                    + list(suite_args.get(suite, [])) + extra), env
        kind, which, _ = RUNTIMES[key[1]]
        if kind == "go":
            return [gobin, "-suite", suite, "-out", out] + ["-quick"] * bool(extra), dict(os.environ)
        rpy = interp[which]
        return ([rpy, "-m", "bench.baselines", "--kind", kind, "--suite", suite,
                 "--out", out] + extra), py_env(rpy, [BENCH_ROOT])

    runs = {}
    t_all = time.monotonic()
    for p in range(args.passes):
        order = keys if p % 2 == 0 else list(reversed(keys))
        for suite in suites:
            for key in order:
                out = os.path.join(out_dir, "%s-%s-%s-p%d.json" % (suite, key[0], key[1], p))
                cmd, env = command(suite, key, out)
                dt = run_cmd(cmd, env, out, args.timeout)
                print("pass %d  %-8s %-24s %5.0fs" % (p, suite, key_name[key], dt), flush=True)
                runs.setdefault((suite, key), []).append(load(out))

    write_summary(out_dir, stamp, runs, suites, keys, key_name, base_key,
                  builds, args.passes, args.note, time.monotonic() - t_all)


def summarize_dir(args):
    """Rebuild summary.md/json from the per-run JSONs already in a directory
    (e.g. after a run was interrupted): keep, per suite, only the passes that
    completed for every column, so no column is compared on fewer passes."""
    import re
    d = args.from_dir
    pat = re.compile(r"^(?P<suite>[a-z]+)-(?P<build>[^-]+)-(?P<cfg>.+)-p(?P<p>\d+)\.json$")
    found = {}
    for fn in sorted(os.listdir(d)):
        m = pat.match(fn)
        if m and (m.group("cfg") in CONFIGS or m.group("cfg") in RUNTIMES):
            found.setdefault(m.group("suite"), {}).setdefault(
                (m.group("build"), m.group("cfg")), {})[int(m.group("p"))] = os.path.join(d, fn)
    suites = [x for x in args.suites.split(",") if x and x in found]
    order_cfg = [c for c in args.configs.split(",") if c]
    order_rt = [r for r in args.runtimes.split(",") if r]
    builds = sorted({k[0] for su in found.values() for k in su} - {RT})
    keys = [(b, c) for b in builds for c in order_cfg] + [(RT, r) for r in order_rt]
    keys = [k for k in keys if any(k in found[su] for su in suites)]
    passes = None
    runs = {}
    for su in suites:
        complete = set.intersection(*[set(found[su].get(k, {})) for k in keys])
        passes = len(complete) if passes is None else min(passes, len(complete))
        for k in keys:
            for p in sorted(complete):
                runs.setdefault((su, k), []).append(load(found[su][k][p]))
        dropped = sorted(set().union(*[set(found[su].get(k, {})) for k in keys]) - complete)
        if dropped:
            print("%s: dropped incomplete pass(es) %s" % (su, dropped))
    key_name = {k: (k[1] if len(builds) == 1 or k[0] == RT else "%s/%s" % k) for k in keys}
    stamp = os.path.basename(os.path.normpath(d))
    write_summary(d, stamp, runs, suites, keys, key_name, keys[0],
                  [(b, "") for b in builds], passes or 0, args.note, None)


def runtime_line(runs, keys, key_name):
    """'threads/asyncio/... on python 3.14.4 (gil off); go on go1.26.6' from the envs."""
    by = {}
    for k in keys:
        if k[0] != RT:
            continue
        docs = next((d for (_, kk), d in runs.items() if kk == k), None)
        if not docs:
            continue
        e = docs[0]["env"]
        if "go" in e:
            what = "%s (GOMAXPROCS = the hub count)" % e["go"]
        else:
            what = "python %s (%s)" % (e["python"], "gil off" if e["gil_enabled"] is False
                                        else "GIL build")
        by.setdefault(what, []).append(key_name[k])
    return "; ".join("%s on %s" % ("/".join(v), w) for w, v in by.items())


def write_summary(out_dir, stamp, runs, suites, keys, key_name, base_key, builds,
                  passes, notes, total_s):
    table = summarize(runs)
    sw_envs = [d["env"] for (_, k), docs in runs.items() if k[0] != RT for d in docs]
    envs = [d["env"] for docs in runs.values() for d in docs]
    first = sw_envs[0] if sw_envs else envs[0]
    loads = sorted(float(e["loadavg"][0]) for e in envs if e.get("loadavg"))
    lines = ["# stackweave runtime comparison %s" % stamp, "",
             "- host: %s, %s, %s vCPU%s" % (
                 first["host"], first.get("cpu_model", "?"), first["nproc"],
                 " (%s)" % first["cpu_perflevels"] if first.get("cpu_perflevels") else "")]
    if sw_envs:
        lines.append(
            "- stackweave %s%s on python %s (%s), %s hubs, netpoll %s, TLBC %s; builds: %s" % (
                first["git_sha"], "-dirty" if first["git_dirty"] else "", first["python"],
                "gil off" if first["gil_enabled"] is False else "GIL ON",
                os.environ.get("STACKWEAVE_BENCH_HUBS", "4"), first["runloom_netpoll"],
                "on" if first["gc_frames_active"] and first["python_tlbc_env"] != "0" else "off",
                ", ".join(("%s=%s" % b) if b[1] else b[0] for b in builds)))
    rl = runtime_line(runs, keys, key_name)
    if rl:
        lines.append("- other runtimes: %s" % rl)
    lines += [
        "- 1-min load average across the runs: %s" % (
            "%.1f .. %.1f" % (loads[0], loads[-1]) if loads else "?"),
        "- %d interleaved pass(es); cells are the median over passes.  Brackets compare "
        "with `%s`: %% for a stackweave config, a ratio (value / stackweave's) for another "
        "runtime; ▲ better / ▼ worse than it, marked only with 2+ passes that all agree and "
        "a gap over 3%%.  `-` = no analogue (see bench/baselines.py, bench/gobench)."
        % (passes, key_name[base_key])]
    lines += ["- %s" % n for n in notes]
    lines.append("")
    for suite in suites:
        t = table.get(suite, {})
        lines.append("## %s" % suite)
        lines.append("")
        lines.append("| bench | %s |" % " | ".join(key_name[k] for k in keys))
        lines.append("|---|%s|" % "|".join("---:" for _ in keys))
        for (kind, name), per_key in t.items():
            lines.append(fmt_row(kind, name, per_key, keys, base_key))
        lines.append("")
    if total_s is not None:
        lines.append("_total %.0f s_" % total_s)
    md = "\n".join(lines) + "\n"
    with open(os.path.join(out_dir, "summary.md"), "w") as f:
        f.write(md)
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump({"stamp": stamp, "builds": builds, "columns": [key_name[k] for k in keys],
                   "suites": suites, "passes": passes, "notes": notes,
                   "table": {s: {"%s|%s" % kn: {key_name[k]: v for k, v in pk.items()}
                                 for kn, pk in t.items()} for s, t in table.items()}},
                  f, indent=2, sort_keys=True)
    print("\n" + md)
    print("wrote %s" % out_dir)


if __name__ == "__main__":
    main()

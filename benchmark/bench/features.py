"""Feature matrix: run benchmark suites once per runtime feature config and
report each config's delta against the default.

Migration-only stackweave (#23) keeps just a few opt-in switches, all read from
the environment when the hubs start, so every config runs in its own process.
Configs are run in interleaved passes (A B C, then C B A, ...) because run-to-
run drift on one box is larger than most real deltas; a delta is only
reported as real when every pass agrees on its sign.

    PYTHONPATH=src:benchmark PYTHON_GIL=0 python -m bench.features
    ... --suites mnsched,echo --configs default,stack-arena --passes 2
    ... --build O2=src --build O3=/path/to/other/tree/src   # A/B two builds
    ... --suite-args mnsched="--no-scaling" --quick

Configs whose feature this box cannot run (io_uring on macOS or without
liburing) are skipped with a note.  Results: one JSON per (suite, build,
config, pass) plus summary.md / summary.json in --out-dir.
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH_ROOT = os.path.dirname(HERE)                  # benchmark/
REPO = os.path.dirname(BENCH_ROOT)

# name -> (env overrides, needs io_uring)
CONFIGS = {
    "default": ({}, False),
    "stack-arena": ({"STACKWEAVE_STACK_ARENA": "1"}, False),
    "optimize-throughput": ({"STACKWEAVE_BENCH_OPTIMIZE": "throughput"}, False),
    "tcpconn-iouring": ({"STACKWEAVE_TCPCONN_IOURING": "1"}, True),
    "iouring-loop": ({"STACKWEAVE_IOURING_LOOP": "1", "STACKWEAVE_IOURING_MS": "0"}, True),
    "iouring-loop-ms": ({"STACKWEAVE_IOURING_LOOP": "1", "STACKWEAVE_IOURING_MS": "1",
                         "STACKWEAVE_IOURING_MS_BUFS": "1024"}, True),
}
DEFAULT_CONFIGS = list(CONFIGS)
SUITES = ("mnsched", "echo")


def iouring_available(py, env):
    code = "import stackweave_c; print(int(stackweave_c.iouring_available()))"
    try:
        out = subprocess.run([py, "-X", "gil=0", "-c", code], env=env,
                             capture_output=True, text=True, timeout=60)
        return out.stdout.strip() == "1"
    except Exception:
        return False


def run_one(py, suite, build_src, cfg_env, out_path, extra_args, timeout):
    env = dict(os.environ)
    # Start from a clean feature slate: an inherited switch would leak into
    # every config, including the "default" it is compared against.
    for k in list(env):
        if k.startswith("STACKWEAVE_") and k not in ("STACKWEAVE_BENCH_HUBS",):
            del env[k]
    env.update(cfg_env)
    env["PYTHONPATH"] = os.pathsep.join([build_src, BENCH_ROOT])
    env["PYTHON_GIL"] = "0"
    cmd = [py, "-X", "gil=0", "-m", "bench." + suite, "--out", out_path] + extra_args
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
                           % (" ".join(cmd[3:]), p.returncode, dt, log, tail))
    return dt


def load(path):
    with open(path) as f:
        return json.load(f)


def summarize(runs, base_key):
    """runs: {(suite, key): [doc, doc, ...]} one doc per pass.
    Returns rows per suite: name -> {key: {"ops": [..], "p50": [..], "p99": [..]}}"""
    table = {}
    for (suite, key), docs in runs.items():
        t = table.setdefault(suite, {})
        for d in docs:
            for r in d.get("results", []):
                t.setdefault(("tput", r["name"]), {}).setdefault(key, []).append(r["ops_per_s"])
            for r in d.get("latency", []):
                t.setdefault(("p50", r["name"]), {}).setdefault(key, []).append(r["p50_us"])
                t.setdefault(("p99", r["name"]), {}).setdefault(key, []).append(r["p99_us"])
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
        else:
            cell = "%.1f us" % med
        if base and k != base_key and len(vals) == len(base):
            # per-pass deltas; a delta counts only if every pass agrees on sign
            ds = [(v - b) / b for v, b in zip(vals, base) if b]
            if ds:
                d = statistics.median(ds) * 100
                agree = len(ds) >= 2 and (all(x > 0 for x in ds) or all(x < 0 for x in ds))
                better = (d > 0) if kind == "tput" else (d < 0)
                mark = ("" if not agree or abs(d) < 3 else (" ▲" if better else " ▼"))
                cell += " (%+.1f%%%s)" % (d, mark)
        cells.append(cell)
    label = name if kind == "tput" else "%s [%s]" % (name, kind)
    return "| %s | %s |" % (label, " | ".join(cells))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--suites", default=",".join(SUITES))
    ap.add_argument("--configs", default=",".join(DEFAULT_CONFIGS))
    ap.add_argument("--build", action="append", default=[],
                    help="NAME=SRC_DIR: an extension build to run (repeatable); "
                         "default one build, 'cur' = this tree's src/")
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--suite-args", action="append", default=[],
                    help='SUITE="args" passed through to that suite')
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--from-dir", default=None,
                    help="re-summarize the runs already in DIR (no new runs); "
                         "only passes complete for every build/config are used")
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
    out_dir = args.out_dir or os.path.join(HERE, "results", "features", stamp)
    os.makedirs(out_dir, exist_ok=True)

    probe_env = dict(os.environ, PYTHONPATH=builds[0][1], PYTHON_GIL="0")
    have_uring = iouring_available(py, probe_env)
    configs = []
    for c in [x for x in args.configs.split(",") if x]:
        if c not in CONFIGS:
            ap.error("unknown config %r (have: %s)" % (c, ", ".join(CONFIGS)))
        if CONFIGS[c][1] and not have_uring:
            print("skip %-20s io_uring not available on this box" % c)
            continue
        configs.append(c)
    keys = [(b, c) for b, _ in builds for c in configs]
    key_name = {k: (k[1] if len(builds) == 1 else "%s/%s" % k) for k in keys}
    base_key = keys[0]
    print("builds: %s\nconfigs: %s\nsuites: %s  passes: %d  out: %s\n"
          % (", ".join("%s=%s" % b for b in builds), ", ".join(configs),
             ", ".join(suites), args.passes, out_dir))

    runs = {}
    t_all = time.monotonic()
    for p in range(args.passes):
        order = keys if p % 2 == 0 else list(reversed(keys))
        for suite in suites:
            extra = list(suite_args.get(suite, [])) + (["--quick"] if args.quick else [])
            for (bname, cname) in order:
                src = dict(builds)[bname]
                out = os.path.join(out_dir, "%s-%s-%s-p%d.json" % (suite, bname, cname, p))
                dt = run_one(py, suite, src, CONFIGS[cname][0], out, extra, args.timeout)
                print("pass %d  %-8s %-6s %-20s %5.0fs" % (p, suite, bname, cname, dt), flush=True)
                runs.setdefault((suite, (bname, cname)), []).append(load(out))

    write_summary(out_dir, stamp, runs, suites, keys, key_name, base_key,
                  [b for b in builds], configs, args.passes, args.note,
                  time.monotonic() - t_all)


def summarize_dir(args):
    """Rebuild summary.md/json from the per-run JSONs already in a directory
    (e.g. after a run was interrupted): keep, per suite, only the passes that
    completed for every build/config key, so no key is compared on fewer
    passes than another."""
    import re
    d = args.from_dir
    pat = re.compile(r"^(?P<suite>[a-z]+)-(?P<build>[^-]+)-(?P<cfg>.+)-p(?P<p>\d+)\.json$")
    found = {}
    for fn in sorted(os.listdir(d)):
        m = pat.match(fn)
        if m and m.group("cfg") in CONFIGS:
            found.setdefault(m.group("suite"), {}).setdefault(
                (m.group("build"), m.group("cfg")), {})[int(m.group("p"))] = os.path.join(d, fn)
    suites = [x for x in args.suites.split(",") if x and x in found]
    order_cfg = [c for c in args.configs.split(",") if c]
    builds = sorted({k[0] for su in found.values() for k in su})
    keys = [(b, c) for b in builds for c in order_cfg
            if any((b, c) in found[su] for su in suites)]
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
    key_name = {k: (k[1] if len(builds) == 1 else "%s/%s" % k) for k in keys}
    stamp = os.path.basename(os.path.normpath(d))
    write_summary(d, stamp, runs, suites, keys, key_name, keys[0],
                  [(b, "(from %s)" % d) for b in builds], [k[1] for k in keys],
                  passes or 0, args.note, None)


def write_summary(out_dir, stamp, runs, suites, keys, key_name, base_key, builds,
                  configs, passes, notes, total_s):
    table = summarize(runs, base_key)
    envs = [d["env"] for docs in runs.values() for d in docs]
    first = envs[0]
    loads = sorted(float(e["loadavg"][0]) for e in envs if e.get("loadavg"))
    lines = ["# stackweave feature matrix %s" % stamp, "",
             "- host: %s, %s, %s vCPU%s, python %s (%s)" % (
                 first["host"], first["cpu_model"], first["nproc"],
                 " (%s)" % first["cpu_perflevels"] if first.get("cpu_perflevels") else "",
                 first["python"],
                 "gil off" if first["gil_enabled"] is False else "GIL ON"),
             "- stackweave %s%s, netpoll %s, TLBC %s" % (
                 first["git_sha"], "-dirty" if first["git_dirty"] else "",
                 first["runloom_netpoll"],
                 "on" if first["gc_frames_active"] and first["python_tlbc_env"] != "0" else "off"),
             "- builds: %s" % ", ".join("%s=%s" % b for b in builds),
             "- 1-min load average across the runs: %s" % (
                 "%.1f .. %.1f" % (loads[0], loads[-1]) if loads else "?"),
             "- %d interleaved pass(es); cells are the median over passes; the delta is "
             "the median per-pass delta vs `%s`, marked ▲ (better) / ▼ (worse) only "
             "with 2+ passes that all agree on its sign and a size over 3%%."
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
        json.dump({"stamp": stamp, "builds": builds, "configs": configs,
                   "suites": suites, "passes": passes, "notes": notes,
                   "table": {s: {"%s|%s" % kn: {key_name[k]: v for k, v in pk.items()}
                                 for kn, pk in t.items()} for s, t in table.items()}},
                  f, indent=2, sort_keys=True)
    print("\n" + md)
    print("wrote %s" % out_dir)


if __name__ == "__main__":
    main()

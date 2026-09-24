#!/usr/bin/env python3
"""cite_drift.py -- model<->source citation-drift linter.

The formal models in `tools/verify/` and the dev docs cite the C source they
correspond to (e.g. `netpoll.c:1158-2195`, `mn_sched_mn_api.c.inc:180-216`, or a
bare `runloom_pump_dispatch_event`).  The `<400-line` code-layout refactor keeps
splitting the monoliths into `*.c.inc` fragments, so line-number citations rot:
`COVERAGE.md` itself flags this as open "Documentation debt".  A stale citation
silently mis-describes what is verified -- the proofs are fine, the *map* lies.

This linter resolves every citation against the live tree and fails on drift:

  * `<file>.c[.inc]:<line>` / `:<l1>-<l2>` where <file> is one of stackweave's OWN
    sources (present in src/runloom_c/): the file must exist AND the line(s) must
    be within range.  Out-of-range or a vanished file  ->  HARD FAIL (drift).
  * the same form where <file> is NOT a stackweave source (a CPython-internal file
    like pystate.c / brc.c / drain.c): classified EXTERNAL.  Resolved against
    $STACKWEAVE_CPYTHON_SRC if set, otherwise reported (not failed) -- those live in
    the patched interpreter, not this repo.
  * a cited `runloom_*` / `m_select`-style symbol that appears nowhere in
    src/runloom_c/  ->  WARN (likely renamed/removed; soft because some are
    macros/CPython symbols).

Cite by **function name**, not line number, to stay drift-proof (per the
verify/README "Add a model" note); this linter is the backstop for the line
citations that remain.

Usage:
    tools/verify/cite_drift.py                  # lint, human report, exit 1 on drift
    tools/verify/cite_drift.py --json           # machine-readable
    STACKWEAVE_CPYTHON_SRC=/path/to/cpython tools/verify/cite_drift.py   # also resolve external

Exit: 0 = no hard drift; 1 = >=1 runloom-file citation is out-of-range/missing.
Wire into scripts/check_all_fast.sh (cheap, no build).
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # repo root
SRC_C = os.path.join(ROOT, "src", "stackweave_c")

# Files/dirs whose comments + prose we scan for citations.
SCAN_DIRS = [HERE]                                     # tools/verify/**
SCAN_FILES = [
    os.path.join(ROOT, "docs", "dev", "cpython_boundary.md"),
    os.path.join(ROOT, "CLAUDE.md"),
]
SCAN_EXTS = (".pml", ".tla", ".c", ".h", ".v", ".als", ".litmus", ".cfg", ".md")

CITE_RE = re.compile(r"\b([A-Za-z_][\w]*\.c(?:\.inc)?):(\d+)(?:-(\d+))?")
SYM_RE = re.compile(r"\b(runloom_[a-z0-9_]+|m_[a-z][a-z0-9_]+)\b")


def runloom_sources():
    """basename -> abspath for every stackweave C source (.c / .c.inc / .h)."""
    out = {}
    if not os.path.isdir(SRC_C):
        return out
    for name in os.listdir(SRC_C):
        if name.endswith((".c", ".c.inc", ".h")):
            out[name] = os.path.join(SRC_C, name)
    return out


def line_count(path):
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return -1


def iter_scan_files():
    for base in SCAN_DIRS:
        for dp, _dn, fns in os.walk(base):
            for fn in fns:
                if fn.endswith(SCAN_EXTS):
                    yield os.path.join(dp, fn)
    for f in SCAN_FILES:
        if os.path.isfile(f):
            yield f


def all_source_text():
    """Concatenated text of every stackweave source, for symbol-existence checks."""
    chunks = []
    for p in runloom_sources().values():
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                chunks.append(f.read())
        except OSError:
            pass
    return "\n".join(chunks)


# Citations pointing the OTHER way: code/scripts/docs naming a doc file.
# Deliberately anchored on "docs/" so a bare "README.md" in prose is ignored.
DOC_CITE_RE = re.compile(r"\bdocs/[A-Za-z0-9_./-]+\.md\b")

# Where a doc citation can appear. Wider than SCAN_DIRS above, because the
# dangling references this catches were in C, in patches and in shell.
DOC_SCAN_DIRS = ("src", "tools", "scripts", "docs")
DOC_SCAN_EXTS = (".c", ".h", ".inc", ".py", ".sh", ".md", ".patch", ".pml",
                 ".tla", ".als", ".cfg", ".txt")

# Frozen debt: doc paths already dangling when this check was introduced.
# Keyed on the DOC PATH, not the citing site, so moving a reference around
# does not resurrect it as "new" -- what matters is whether the target got
# written, and the count can only go down.
DOC_BASELINE = os.path.join(HERE, "doc_citation_baseline.json")


def check_doc_citations():
    """Find `docs/**.md` paths cited from anywhere in the tree that do not exist.

    The mirror image of the check above, and the gap that let six dangling
    references survive: cite_drift resolved DOCS citing missing SOURCE, and
    nothing resolved SOURCE citing a missing DOC. docs/dev/rr_vpmu_status.md
    was named by the rr patch, its build script, the vPMU probe,
    tools/README.md and two soak stages -- and had never been written. The
    rationale for a real hack lived only in a patch comment, findable solely
    by someone who already knew to look there.

    NEW ones are a hard failure; a KNOWN BACKLOG is reported and tolerated.
    That split is not softness -- it is the only way this check could be
    switched on at all. Preparing the public tree (commit 6911220) dropped
    docs/dev/** and left ~85 references behind, so failing on the total would
    have meant a permanently red gate, i.e. a check everyone disables. The
    baseline (doc_citation_baseline.json) freezes that debt so the number can
    only go DOWN, while any newly-introduced dangling reference -- the
    rr_vpmu_status.md case -- fails immediately.

    Same shape as the supply-chain lane's "bandit: NEW vs baseline".
    """
    missing = []
    for d in DOC_SCAN_DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in sorted(dirnames)
                           if x not in (".git", "__pycache__", "obj", "build")]
            for name in sorted(filenames):
                if not name.endswith(DOC_SCAN_EXTS):
                    continue
                fpath = os.path.join(dirpath, name)
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                for m in DOC_CITE_RE.finditer(text):
                    cited = m.group(0)
                    if os.path.isfile(os.path.join(ROOT, cited)):
                        continue
                    lineno = text.count("\n", 0, m.start()) + 1
                    missing.append(("{0}:{1}".format(
                        os.path.relpath(fpath, ROOT), lineno), cited))
    return missing


def main(argv):
    as_json = "--json" in argv
    sources = runloom_sources()
    src_line_counts = {n: line_count(p) for n, p in sources.items()}
    cpy_src = os.environ.get("STACKWEAVE_CPYTHON_SRC")

    drift = []        # hard failures (stackweave file, out of range / missing)
    external = []     # cpython-internal citations
    ok = 0
    cited_syms = set()

    for fpath in iter_scan_files():
        rel = os.path.relpath(fpath, ROOT)
        try:
            with open(fpath, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for m in CITE_RE.finditer(text):
            base, l1s, l2s = m.group(1), m.group(2), m.group(3)
            l1 = int(l1s)
            l2 = int(l2s) if l2s else l1
            lineno = text.count("\n", 0, m.start()) + 1
            where = "{0}:{1}".format(rel, lineno)
            if base in sources:
                n = src_line_counts[base]
                if n < 0:
                    drift.append((where, "{0}:{1}".format(base, l1s),
                                  "unreadable source"))
                elif l1 > n or l2 > n:
                    drift.append((where, m.group(0),
                                  "out of range (file has {0} lines)".format(n)))
                else:
                    ok += 1
            else:
                resolved = None
                if cpy_src:
                    cand = os.path.join(cpy_src, base)
                    if os.path.isfile(cand):
                        n = line_count(cand)
                        resolved = (l1 <= n and l2 <= n)
                external.append((where, m.group(0), resolved))
        for m in SYM_RE.finditer(text):
            cited_syms.add(m.group(1))

    src_text = all_source_text()
    missing_syms = sorted(s for s in cited_syms if s not in src_text)
    missing_docs = check_doc_citations()
    try:
        with open(DOC_BASELINE) as _fh:
            _known = set(json.load(_fh).get("known_missing", []))
    except Exception:
        _known = set()
    new_docs = [(w, d) for w, d in missing_docs if d not in _known]
    old_docs = [(w, d) for w, d in missing_docs if d in _known]

    if as_json:
        print(json.dumps({
            "ok_citations": ok,
            "drift": [{"where": w, "cite": c, "why": y} for w, c, y in drift],
            "external": [{"where": w, "cite": c, "resolved": r}
                         for w, c, r in external],
            "missing_symbols": missing_syms,
            "missing_docs_new": [{"where": w, "doc": d} for w, d in new_docs],
            "missing_docs_known": len(old_docs),
        }, indent=1))
    else:
        print("cite_drift: {0} runloom-file citations OK, {1} DRIFTED, "
              "{2} external, {3} unresolved symbols, {4} NEW missing docs "
              "({5} known backlog)"
              .format(ok, len(drift), len(external), len(missing_syms),
                      len(new_docs), len(old_docs)))
        if drift:
            print("\nHARD DRIFT (runloom-source citations that no longer resolve):")
            for w, c, y in drift:
                print("  {0:<48} {1:<32} {2}".format(w, c, y))
        ext_unresolved = [(w, c) for w, c, r in external if r is False]
        if ext_unresolved:
            print("\nEXTERNAL citations out of range vs $STACKWEAVE_CPYTHON_SRC:")
            for w, c in ext_unresolved:
                print("  {0:<48} {1}".format(w, c))
        if missing_syms:
            print("\nWARN: cited symbols not found in src/runloom_c/ "
                  "(renamed/removed, or a macro/CPython symbol):")
            for s in missing_syms[:40]:
                print("  {0}".format(s))
            if len(missing_syms) > 40:
                print("  ... +{0} more".format(len(missing_syms) - 40))
        if not cpy_src and external:
            print("\n(set STACKWEAVE_CPYTHON_SRC=/path/to/cpython to also resolve "
                  "the {0} external cpython-internal citations)".format(len(external)))

        if new_docs:
            print("\nNEW MISSING DOCS (cited from the tree, never written):")
            for w, d in new_docs:
                print("  {0:<48} {1}".format(w, d))
            print("  -> write the doc, or fix the path. Only add to {0} if the\n"
                  "     doc is genuinely expected-absent -- never to silence a\n"
                  "     reference you just introduced.".format(
                      os.path.relpath(DOC_BASELINE, ROOT)))

    return 1 if (drift or new_docs) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

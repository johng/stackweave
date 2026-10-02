#!/usr/bin/env python3
"""Check that no function in the stackweave extension uses a thread-local's
address, or the thread pointer, after a call that can park the fiber.

A parked fiber can resume on another hub thread.  A thread-local's address is
per OS thread, but clang treats it as constant for the whole function: on
Darwin it resolves the address once (a call through the variable's TLV
descriptor) and keeps it in a register or a stack slot for every later access
in that function, across calls.  If one of those calls parks the fiber and it
resumes on another hub, the later accesses touch the ORIGIN thread's copy and
race that thread.  This is the exec-home bug class (src/patches/README.md) in
stackweave's own C: TSan finding A2 (docs/dev/TSAN.md), where
runloom_chan_select advanced the origin hub's select PRNG after a migration.
The thread pointer (`mrs TPIDRRO_EL0`, the inlined `_Py_ThreadId()` in every
Py_INCREF / Py_DECREF) is the same hazard and is checked the same way.

On the extension's arm64 machine code:

1. Sources.  A TLS address is born at a call through a `__thread_vars`
   descriptor (`adrp/add xD, desc; ldr xN, [xD]; blr xN` with x0 = xD), at a
   read of the thread pointer, and at a call to a function in the image that
   returns one.
2. May-park calls.  runloom_coro_yield suspends the fiber (every
   runloom_asm_swap caller is checked against a fixed set).  A call may park
   if it reaches that function; if it is an indirect call, except through a
   pointer in NONPARK_POINTERS; if it enters the Python C API, which can run
   Python code (a finalizer, a callback, the preemption hook), except the
   functions in NONPARK_EXTERNALS; or if it is a libc call that calls back
   into the image.  Computed over the image's call graph.
3. Uses after a park.  A forward data-flow pass over each function with a
   source follows TLS addresses through registers (FP/SIMD included), stack
   slots, and pointer arithmetic (add/sub, and/orr/bic, madd, csel, moves).
   A may-park call marks every value live across it STALE.  A stale value
   reaching a sink is a finding: a load or store address, a compare or test,
   an argument the callee reads, an indirect branch target, a store to
   memory, or a return value.  A fresh TLS address that leaves the frame (is
   stored to non-stack memory, or is passed to a call that may park) is a
   finding too, since the check cannot follow it.

Two reviewed lists narrow the result.  NATIVE_ONLY functions run only on
their own OS thread's stack, never a fiber's (thread start routines and
hooks; the check fails if one gains a direct caller), so they are skipped.
ACCEPTED records stale uses that are real exposures in a narrow case and are
left as they are; it is keyed by function and variable, so anything new in
those functions still fails.

Exit 0: no finding outside ACCEPTED.  Exit 1: a finding; the report names the
function, the variable, the instruction, and the call that made the value
stale (-v adds why that call may park).  Exit 2: it cannot vouch: not arm64
Darwin, a tool failure, a stripped image, a control-flow or TLV form it does
not model in a function that touches TLS, or an anchor it should have found
and did not.

Limits.  Pointer arithmetic beyond the operations above is taken to produce a
non-pointer.  The pass follows data flow, not equalities: after
`if (x == tid)` the compiler may use x where it would have re-read tid.  It is
path-insensitive, so a register holding a TLS address on one path can be
reported where a correlated path the program cannot take reaches a use (seen
at -O1).  A TLS address passed to a call that cannot park is not followed into
the callee (-v lists those calls); one stored in a stack struct whose address
a parking callee receives is not seen; one returned in x1 rather than x0 is
lost.  A value stored through an alias of the function's own frame and read
back by a direct slot access is not followed.  Values loaded FROM a
thread-local (a cached pointer) are out of scope.  Data flow inside the Python
C API and libc is not checked.  -Oz (outlined helpers with control flow),
-flto=thin and GCC's emulated TLS are not modelled: exit 2.  arm64 Mach-O
only; on Linux nothing checks this class, only the out-of-line accessors
(RUNLOOM_NOINLINE) protect it.

usage: check_tls_after_park.py [-v] [EXTENSION.so]
       (default: the stackweave_c extension built into src/)
"""
import glob
import os
import platform
import re
import struct
import subprocess
import sys
import tempfile
from collections import defaultdict

# The one call that suspends a running fiber.
PARK_ROOTS = {"_runloom_coro_yield"}
# Every runloom_asm_swap caller.  _runloom_coro_resume swaps into a fiber and
# returns on the same thread when it yields; _runloom_asm_entry leaves a
# finished fiber for good.  Any other caller is a suspension primitive this
# check does not model.
SWAP = "_runloom_asm_swap"
SWAP_CALLERS = {"_runloom_coro_yield", "_runloom_coro_resume", "_runloom_asm_entry"}

# Python C API functions that cannot run Python code or a finalizer, so cannot
# park the calling fiber.  Everything else under _Py / __Py may.
NONPARK_EXTERNALS = {
    # raw and object allocators: no GC run, no Python code
    "_PyMem_RawMalloc", "_PyMem_RawCalloc", "_PyMem_RawRealloc",
    "_PyMem_RawFree", "_PyMem_Malloc", "_PyMem_Calloc", "_PyMem_Realloc",
    "_PyMem_Free",
    # thread-state queries and creation (no audit hook, no Python code)
    "_PyThreadState_Get", "_PyThreadState_GetUnchecked",
    "__PyThreadState_GetCurrent", "_PyThread_get_thread_ident",
    "_PyThreadState_New",
    # read a flag or copy a struct
    "_PyErr_Occurred", "__Py_IsFinalizing", "_Py_IsFinalizing",
    "_PyObject_GetArenaAllocator",
    # never returns
    "__Py_FatalErrorFunc",
    # block the OS thread, never switch the fiber
    "_PyMutex_Lock", "_PyMutex_Unlock",
}

# Function pointers whose targets cannot park: a named global, or a field an
# external fills in through an out-parameter (OUT_POINTERS).
NONPARK_POINTERS = {
    "_runloom_coro_pre_swap":
        "re-arms the C-stack limit for the fiber being resumed",
    "PyObjectArenaAllocator.alloc": "the arena allocator (mmap by default)",
    "PyObjectArenaAllocator.free": "the arena allocator (munmap by default)",
}
# External -> {offset in the struct x0 points to: the pointer it stores there}
OUT_POINTERS = {
    "_PyObject_GetArenaAllocator": {8: "PyObjectArenaAllocator.alloc",
                                    16: "PyObjectArenaAllocator.free"},
}

# Integer arguments of external calls, where fewer than eight: registers past
# them are leftovers, not arguments.  Any other external reads x0-x7.
STUB_ARGS = dict.fromkeys(
    ("_PyGILState_Ensure", "_PyThreadState_Get", "_PyThreadState_GetUnchecked",
     "__PyThreadState_GetCurrent", "_PyErr_Occurred", "_PyErr_Clear",
     "_PyErr_NoMemory", "_PyErr_CheckSignals", "_PyEval_SaveThread",
     "_PyEval_GetBuiltins", "_PyContext_CopyCurrent", "_Py_IsFinalizing",
     "_Py_GetRecursionLimit", "_PyThread_get_thread_ident", "___error",
     "___stack_chk_fail", "_abort", "_pthread_self", "_getpid", "_fork"), 0)
STUB_ARGS.update(dict.fromkeys(
    ("_PyGILState_Release", "_PyEval_RestoreThread", "__Py_HandlePending",
     "_PyMutex_Lock", "_PyMutex_Unlock", "_PyMem_Free", "_PyMem_RawFree",
     "_free", "__Py_Dealloc", "__Py_DecRefShared", "__Py_MergeZeroLocalRefcount",
     "_PyObject_GetArenaAllocator"), 1))

# libc calls that call back into the image.
CALLBACK_EXTERNALS = {"_qsort", "_qsort_r", "_bsearch", "_pthread_once",
                      "_dispatch_once_f", "_dispatch_sync_f", "_atexit"}

# Functions whose frames only ever live on their own OS thread's stack, never
# a fiber's, so no call there can move them to another thread.  Each is
# reached only through the pointer its reason names: the check fails if one
# gains a direct caller.
NATIVE_ONLY = {
    "_runloom_hub_main": "an M:N hub thread's start routine (pthread_create)",
    "_py_blocking_worker_thread_fini":
        "the blocking-pool worker's exit hook "
        "(runloom_blockpool_worker_thread_fini), run on that worker thread",
    "_runloom_blockpool_worker":
        "a blocking-pool worker thread's start routine (runloom_thread_create)",
}

# Reviewed findings left as they are: function -> (variables, why).  Each is
# a real exposure in a narrow case.  Only stale uses of the listed variables
# are accepted, so anything new in the same function still fails.
ACCEPTED = {
    "_runloom_sim_dispatch_due_plane": (
        {"_runloom_sim_due_scratch"},
        "deterministic-simulation mode only: reached on a fiber through "
        "netpoll_poll(), and parks only if a woken g's last reference runs a "
        "finalizer that parks"),
}

# A function that must resolve a thread-local, or the scan is blind.
ANCHORS = {"_runloom_mn_tls_current_g"}

THREAD_POINTER = "thread pointer"

FUNC_RE = re.compile(r"^([0-9a-f]+) <(.+)>:$")
INSN_RE = re.compile(r"^\s+([0-9a-f]+):\s+(\S+)(?:\s+(.*?))?\s*$")
SECTION_RE = re.compile(r"^Disassembly of section ([^:]+):")
IMM_RE = re.compile(r"^#(-?(?:0x[0-9a-f]+|\d+))$")
ADDR_RE = re.compile(r"^0x([0-9a-f]+)$")
COND_BRANCH_RE = re.compile(r"^b\.\w+$")

ARGS = ["x%d" % i for i in range(8)] + ["v%d" % i for i in range(8)]
CALLER_SAVED = ["x%d" % i for i in range(19)] + ["x30"] + \
    ["v%d" % i for i in list(range(8)) + list(range(16, 32))]

COMPARES = {"cmp", "cmn", "tst", "ccmp", "ccmn", "fcmp", "fcmpe", "fccmp",
            "fccmpe"}
NO_DEST = {"nop", "dmb", "dsb", "isb", "hint", "yield", "clrex", "msr",
           "prfm", "prfum", "bti", "pacibsp", "autibsp", "paciasp", "autiasp",
           "sev", "sevl", "wfe", "wfi", "dc", "ic", "sys", "csdb", "ssbb",
           "pssbb", "esb"}
TRAPS = {"brk", "udf", "hlt"}
TESTS = {"cbz", "cbnz", "tbz", "tbnz"}
# Operations that can carry a pointer from a source to the destination.
PTR_OPS = {"mov", "orr", "add", "adds", "sub", "subs", "and", "ands", "bic",
           "bics", "madd", "msub", "smaddl", "umaddl", "csel", "csinc",
           "csinv", "csneg", "fmov", "ins", "dup", "umov", "fcsel"}
# Operations that also read their destination register.
READS_DEST = {"movk", "bfi", "bfxil", "bfm", "ins", "fmla", "fmls", "mla",
              "mls", "bsl", "bit", "bif", "sli", "sri", "tbx"}
RMW_PREFIXES = ("swp", "cas", "ldadd", "ldclr", "ldeor", "ldset", "ldsmax",
                "ldsmin", "ldumax", "ldumin", "stadd", "stclr", "steor",
                "stset", "stsmax", "stsmin", "stumax", "stumin")


class ScanError(Exception):
    """The check cannot vouch for this image."""


def run(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        raise ScanError("cannot run %s: %s" % (cmd[0], e))
    if p.returncode != 0:
        raise ScanError("%s failed (rc=%d): %s"
                        % (" ".join(cmd[:2]), p.returncode, p.stderr.strip()[:200]))
    return p.stdout


def thin_arm64(binary, tmpdir):
    info = run(["lipo", "-info", binary])
    if "Non-fat file" in info:
        return binary
    out = os.path.join(tmpdir, "arm64-slice")
    run(["lipo", "-thin", "arm64", "-output", out, binary])
    return out


# ------------------------------------------------------------------ image --

class Func:
    def __init__(self, name, start):
        self.name, self.start = name, start
        self.insns = []          # (addr, mnemonic, [operands])
        self.index = {}          # addr -> position in insns

    def add(self, addr, mnem, ops):
        ops = re.sub(r"\s*<[^>]*>", "", ops.split(";")[0]).strip()
        self.index[addr] = len(self.insns)
        self.insns.append((addr, mnem, split_ops(ops)))

    @property
    def end(self):
        return self.insns[-1][0] if self.insns else self.start

    @property
    def outlined(self):
        return self.name.startswith("_OUTLINED_FUNCTION_")


class Image:
    def __init__(self, path):
        self.path = path
        self.tlv = {}            # TLV descriptor address -> variable
        self.stubs = {}          # stub address -> external symbol
        self.data_syms = {}      # data address -> symbol (globals, GOT slots)
        self.sections = []       # (addr, size, file offset)
        self.funcs = []
        self.by_addr = {}
        with open(path, "rb") as fh:
            self.data = fh.read()
        self._symbols()
        self._sections()
        self._code()

    def _symbols(self):
        for line in run(["nm", "-m", self.path]).splitlines():
            parts = line.split()
            if len(parts) < 3 or not re.match(r"^[0-9a-f]+$", parts[0]):
                continue
            a = int(parts[0], 16)
            if "(__DATA,__thread_vars)" in line:
                self.tlv[a] = parts[-1]
            elif "(__DATA" in line:
                self.data_syms[a] = parts[-1]
        section = None
        for line in run(["otool", "-Iv", self.path]).splitlines():
            if line.startswith("Indirect symbols for"):
                section = line.split("(")[1].split(")")[0]
                continue
            parts = line.split()
            if len(parts) != 3 or not parts[0].startswith("0x"):
                continue
            if section == "__TEXT,__stubs":
                self.stubs[int(parts[0], 16)] = parts[2]
            elif section.endswith(",__got"):
                self.data_syms[int(parts[0], 16)] = parts[2]

    def _sections(self):
        cur = {}
        for line in run(["otool", "-l", self.path]).splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            if parts[0] == "sectname":
                cur = {}
            elif parts[0] in ("addr", "size", "offset"):
                cur[parts[0]] = int(parts[1], 0)
                if len(cur) == 3:
                    self.sections.append((cur["addr"], cur["size"], cur["offset"]))
                    cur = {"done": 1}

    def read(self, addr, size, signed):
        for base, length, off in self.sections:
            if off and base <= addr and addr + size <= base + length:
                fmt = "<" + {1: "b", 2: "h", 4: "i"}[size]
                if not signed:
                    fmt = fmt.upper()
                return struct.unpack_from(fmt, self.data, off + addr - base)[0]
        return None

    def _code(self):
        func, in_text = None, False
        out = run(["objdump", "-d", "--no-show-raw-insn", self.path])
        for line in out.splitlines():
            m = SECTION_RE.match(line)
            if m:
                in_text, func = m.group(1) == "__TEXT,__text", None
                continue
            if not in_text:
                continue
            m = FUNC_RE.match(line)
            if m:
                func = Func(m.group(2), int(m.group(1), 16))
                self.funcs.append(func)
                self.by_addr[func.start] = func
                continue
            m = INSN_RE.match(line)
            if m and func is not None:
                func.add(int(m.group(1), 16), m.group(2), m.group(3) or "")
        self.funcs = [f for f in self.funcs if f.insns]
        if not self.funcs:
            raise ScanError("no functions in __TEXT,__text (stripped image?)")


def split_ops(ops):
    out, depth, cur = [], 0, ""
    for ch in ops:
        depth += ch in "[{"
        depth -= ch in "]}"
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


# --------------------------------------------------------------- operands --

def reg(tok):
    """Canonical register for an operand: 'xN', 'vN', 'sp', '' for the zero
    register, None if the operand is not a register."""
    tok = tok.strip()
    m = re.match(r"^[xw](\d+)$", tok)
    if m:
        return "x" + m.group(1)
    if tok in ("sp", "wsp"):
        return "sp"
    if tok in ("xzr", "wzr"):
        return ""
    if tok in ("fp", "lr"):
        return "x29" if tok == "fp" else "x30"
    m = re.match(r"^(?:[bhsdq](\d+)|v(\d+)(?:\.\w+)?(?:\[\d+\])?)$", tok)
    if m:
        return "v" + (m.group(1) or m.group(2))
    return None


def regs_in(tok):
    """Every register an operand reads or names: a register list's members,
    and a memory operand's base and index (an address is a read too)."""
    if tok.startswith("{"):
        inner = tok.split("}")[0].strip("{ ")
        return [r for r in (reg(t) for t in inner.split(",")) if r]
    if tok.startswith("["):
        parts = [p.strip() for p in tok.rstrip("!").strip("[]").split(",")]
        return [r for r in (reg(p) for p in parts[:2]) if r]
    r = reg(tok)
    return [r] if r else []


def imm(tok):
    m = IMM_RE.match(tok.strip())
    return int(m.group(1), 0) if m else None


def width(tok):
    tok = tok.strip()
    if re.match(r"^q\d+$", tok):
        return 16
    if re.match(r"^(w\d+|wzr|wsp|s\d+)$", tok):
        return 4
    if re.match(r"^h\d+$", tok):
        return 2
    if re.match(r"^b\d+$", tok):
        return 1
    return 8


def access_width(mnem, tok):
    """Bytes a load/store moves per register operand."""
    if mnem.endswith("sw"):
        return 4                         # ldrsw / ldursw / ldpsw
    if mnem.endswith("b"):
        return 1
    if mnem.endswith("h"):
        return 2
    return width(tok)


class Mem:
    """[base, #off] / [base, index{, ext #s}] with pre- or post-index."""

    def __init__(self, tok, post):
        self.writeback = tok.endswith("!")
        parts = [p.strip() for p in tok.rstrip("!").strip("[]").split(",")]
        self.base = reg(parts[0])
        self.off, self.index = 0, None
        if len(parts) > 1:
            if parts[1].startswith("#"):
                self.off = imm(parts[1])
            else:
                self.index = reg(parts[1])
        self.post = post


# ----------------------------------------------------------------- values --
# A register or stack slot holds one of:
#   ('T', vars, park)  a TLS address or the thread pointer, or a pointer
#                      derived from one; park is the address of the may-park
#                      call that made it stale, None while fresh
#   ('S', off)         the stack address entry_sp + off
#   ('K', c)           the constant c (adrp/adr results and offsets)
#   ('D', var)         a TLV descriptor's address
#   ('H', var)         that descriptor's thunk pointer
#   ('P', sym)         a word loaded from the named global or GOT slot
#   ('E', table, size, signed)            an entry loaded from a jump table
#   ('J', base, table, size, signed, sh)  the target computed from it
# A missing entry means unknown, and not derived from a TLS address.

def is_t(v):
    return v is not None and v[0] == "T"


def join(a, b):
    if a == b:
        return a
    if is_t(a) or is_t(b):
        va = a[1] if is_t(a) else frozenset()
        vb = b[1] if is_t(b) else frozenset()
        parks = [v[2] for v in (a, b) if is_t(v) and v[2] is not None]
        return ("T", va | vb, min(parks) if parks else None)
    return None


def taint_of(vals):
    out = None
    for v in vals:
        if is_t(v):
            out = join(out, v) if out else v
    return out


class State:
    __slots__ = ("regs", "slots", "sp")

    def __init__(self, regs=None, slots=None, sp=0):
        self.regs = regs if regs is not None else {}
        self.slots = slots if slots is not None else {}
        self.sp = sp

    def copy(self):
        return State(dict(self.regs), dict(self.slots), self.sp)

    def joined(self, other):
        """self joined with other, or None when that adds nothing."""
        regs, slots = {}, {}
        for k in set(self.regs) | set(other.regs):
            v = join(self.regs.get(k), other.regs.get(k))
            if v is not None:
                regs[k] = v
        for k in set(self.slots) | set(other.slots):
            v = join(self.slots.get(k), other.slots.get(k))
            if v is not None:
                slots[k] = v
        sp = self.sp if self.sp == other.sp else None
        if regs == self.regs and slots == self.slots and sp == self.sp:
            return None
        return State(regs, slots, sp)

    def get(self, r):
        if r == "sp":
            return ("S", self.sp)        # offset None: somewhere in the frame
        return self.regs.get(r) if r else None

    def set(self, r, v):
        if r == "sp":
            self.sp = v[1] if v is not None and v[0] == "S" else None
        elif r:
            if v is None:
                self.regs.pop(r, None)
            else:
                self.regs[r] = v


class Finding:
    def __init__(self, func, addr, kind, what, val):
        self.func, self.addr, self.kind, self.what = func, addr, kind, what
        self.vars, self.park = val[1], val[2]

    def __str__(self):
        s = "%s+0x%x: %s %s [%s]" % (
            self.func.name, self.addr - self.func.start, self.kind, self.what,
            ", ".join(sorted(v.lstrip("_") for v in self.vars)))
        if self.park is not None:
            s += ", stale since the call at +0x%x" % (self.park - self.func.start)
        return s


# --------------------------------------------------------------- analysis --

class Analyzer:
    def __init__(self, img):
        self.img = img
        self.may_park = {}       # function start -> None, a reason, or the
                                 # start of a may-park callee
        self.arg_reads = {}      # function start -> set of arg registers read
        self.returns_tls = {}    # function start -> frozenset of vars
        self.passed_fresh = []   # (function, addr, callee, vars)
        self.sites = {}          # (function start, addr) -> {(kind, x)}

    # ---- call targets ----
    def target(self, f, i):
        """('func', Func) / ('stub', name) / ('indirect', site) for a call or
        tail call at insns[i], else (None, None).  An indirect site is what the
        classifying pass saw in its target register: a TLV thunk, a pointer
        loaded from a named global, or an unknown register."""
        addr, mnem, ops = f.insns[i]
        if mnem in ("blr", "br"):
            return "indirect", self.sites.get((f.start, addr), {("reg", None)})
        if mnem != "bl" and mnem != "b" and not COND_BRANCH_RE.match(mnem) \
                and mnem not in TESTS:
            return None, None
        m = ADDR_RE.match(ops[-1]) if ops else None
        if not m:
            return None, None
        t = int(m.group(1), 16)
        if mnem != "bl" and f.start <= t <= f.end:
            return None, None
        if t in self.img.stubs:
            return "stub", self.img.stubs[t]
        if t in self.img.by_addr:
            return "func", self.img.by_addr[t]
        raise ScanError("%s+0x%x: %s to 0x%x, which is not a function start"
                        % (f.name, addr - f.start, mnem, t))

    def describe_call(self, f, addr):
        """The call at addr and why it may park, as a chain of callees."""
        kind, t = self.target(f, f.index[addr])
        if kind == "stub":
            return "%s: %s" % (t, self.stub_may_park(t))
        if kind != "func":
            return "an indirect call through %s" % ", ".join(
                sorted(str(x[1] or "a register") for x in t))
        chain, why = [t.name], self.may_park.get(t.start)
        while isinstance(why, int) and len(chain) < 32:
            chain.append(self.img.by_addr[why].name)
            why = self.may_park.get(why)
        return " -> ".join([c.lstrip("_") for c in chain] + [why or "?"])

    @staticmethod
    def site_may_park(site):
        return any(k == "reg" or (k == "ptr" and x not in NONPARK_POINTERS)
                   for k, x in site)

    def classify_sites(self):
        """Run the data flow once over every function, before any call is
        known to park, to see what each indirect call goes through."""
        self.sites, self.unmodelled = {}, []
        for f in self.img.funcs:
            if f.outlined:
                continue
            try:
                self.analyze(f)
            except ScanError as e:
                if self.mentions_tls(f):
                    raise
                self.unmodelled.append("%s (%s)" % (f.name, e))
                for addr, mnem, ops in f.insns:   # unknown: may park
                    if mnem in ("blr", "br"):
                        self.sites[(f.start, addr)] = {("reg", None)}

    def mentions_tls(self, f):
        page = {}
        for addr, mnem, ops in f.insns:
            if mnem == "mrs" and ops[1].upper().startswith("TPIDR"):
                return True
            if mnem == "adrp":
                page[reg(ops[0])] = int(ADDR_RE.match(ops[1]).group(1), 16)
            elif mnem == "add" and len(ops) == 3 and reg(ops[1]) in page \
                    and imm(ops[2]) is not None:
                if page[reg(ops[1])] + imm(ops[2]) in self.img.tlv:
                    return True
        return False

    def site(self, f, addr, kind, x):
        self.sites.setdefault((f.start, addr), set()).add((kind, x))

    def stub_may_park(self, name):
        if name in CALLBACK_EXTERNALS:
            return "calls back into the image"
        if name.startswith(("_Py", "__Py")) and name not in NONPARK_EXTERNALS:
            return "enters the Python C API"
        return None

    def build_call_graph(self):
        callees = defaultdict(set)
        swap_callers, direct_callers = set(), defaultdict(set)
        for f in self.img.funcs:
            why = "suspends the fiber" if f.name in PARK_ROOTS else None
            for i, (addr, mnem, ops) in enumerate(f.insns):
                kind, t = self.target(f, i)
                if kind == "func":
                    callees[f.start].add(t.start)
                    direct_callers[t.name].add(f.name)
                    if t.name == SWAP:
                        swap_callers.add(f.name)
                elif kind == "stub" and self.stub_may_park(t) and not why:
                    why = "%s (%s)" % (t, self.stub_may_park(t))
                elif kind == "indirect" and not why and self.site_may_park(t):
                    why = "an indirect call at +0x%x" % (addr - f.start)
            self.may_park[f.start] = why
        unknown = swap_callers - SWAP_CALLERS
        if unknown:
            raise ScanError("%s is called from %s: a suspension primitive "
                            "this check does not model" % (SWAP, sorted(unknown)))
        if not any(f.name in PARK_ROOTS for f in self.img.funcs):
            raise ScanError("no %s in the image" % ", ".join(sorted(PARK_ROOTS)))
        for name in NATIVE_ONLY:
            if direct_callers.get(name):
                raise ScanError("NATIVE_ONLY %s has direct callers %s"
                                % (name, sorted(direct_callers[name])))
        changed = True
        while changed:
            changed = False
            for s, cs in callees.items():
                if self.may_park[s]:
                    continue
                for c in cs:
                    if self.may_park.get(c):
                        self.may_park[s] = c
                        changed = True
                        break

    # ---- which argument registers a function reads ----
    def compute_arg_reads(self):
        for f in self.img.funcs:
            self.arg_reads[f.start] = None
        changed = True
        while changed:
            changed = False
            for f in self.img.funcs:
                r = self.reads_at_entry(f)
                if r != self.arg_reads[f.start]:
                    self.arg_reads[f.start] = r
                    changed = True

    def callee_reads(self, kind, t):
        if kind == "func":
            r = self.arg_reads.get(t.start)
            return r if r is not None else set()
        if kind == "indirect" and t == {("tlv", None)}:
            return {"x0"}
        if kind == "stub" and t in STUB_ARGS:
            return {"x%d" % i for i in range(STUB_ARGS[t])}
        return set(ARGS)

    def reads_at_entry(self, f):
        """Argument registers some path reads before writing (must-written
        forward analysis; unreached code counts as reading everything)."""
        n = len(f.insns)
        defined = [None] * n
        defined[0] = frozenset()
        work, reads = [0], set()
        while work:
            i = work.pop()
            d = set(defined[i])
            addr, mnem, ops = f.insns[i]
            succ = [i + 1]
            kind, t = self.target(f, i)
            used, written = self.uses_defs(f, i)
            reads |= {r for r in used if r in ARGS and r not in d}
            if kind:
                reads |= {r for r in self.callee_reads(kind, t)
                          if r in ARGS and r not in d}
                d |= set(CALLER_SAVED)
                if mnem in ("b", "br"):
                    succ = []
            elif mnem in ("ret",) or mnem in TRAPS:
                succ = []
            elif mnem == "b":
                succ = [f.index.get(int(ADDR_RE.match(ops[-1]).group(1), 16))]
            elif mnem == "br":
                succ = []
            elif COND_BRANCH_RE.match(mnem) or mnem in TESTS:
                succ.append(f.index.get(int(ADDR_RE.match(ops[-1]).group(1), 16)))
            d |= written
            for j in succ:
                if j is None or j >= n:
                    continue
                nd = frozenset(d) if defined[j] is None else defined[j] & d
                if nd != defined[j]:
                    defined[j] = nd
                    work.append(j)
        for i in range(n):
            if defined[i] is None:          # reached only through a jump table
                used, _ = self.uses_defs(f, i)
                reads |= {r for r in used if r in ARGS}
                kind, t = self.target(f, i)
                if kind:
                    reads |= self.callee_reads(kind, t) & set(ARGS)
        return reads

    def uses_defs(self, f, i):
        addr, mnem, ops = f.insns[i]
        if not ops:
            return set(), set()
        if mnem.startswith(("st", "swp", "cas")) or mnem in COMPARES or \
                mnem in TESTS or mnem in ("br", "blr", "ret") or mnem in NO_DEST:
            used = {r for o in ops for r in regs_in(o)}
            written = set()
            if mnem.startswith(("stxr", "stlxr", "stxp", "stlxp")):
                written = set(regs_in(ops[0]))
            return used, written
        mi = next((k for k, o in enumerate(ops) if o.startswith("[")), None)
        if mi is not None:                   # a load: data regs written
            written = {r for o in ops[:mi] for r in regs_in(o)}
            used = {r for o in ops[mi:] for r in regs_in(o)}
            if mnem.startswith(RMW_PREFIXES):
                used |= set(regs_in(ops[0]))
            return used, written
        written = set(regs_in(ops[0]))
        used = {r for o in ops[1:] for r in regs_in(o)}
        if mnem in READS_DEST:
            used |= written
        return used, written

    # ---- data flow ----
    def has_source(self, f):
        for i, (addr, mnem, ops) in enumerate(f.insns):
            if mnem == "mrs" and ops[1].upper().startswith("TPIDR"):
                return True
            kind, t = self.target(f, i)
            if kind == "indirect" and ("tlv", None) in t:
                return True
            if kind == "func" and (t.start in self.returns_tls or
                                   (t.outlined and self.has_source(t))):
                return True
        return False

    def analyze(self, f):
        self.findings, self.ret = {}, set()
        n = len(f.insns)
        states = [None] * n
        states[0] = State()
        work = [0]
        while work:
            i = work.pop()
            for j, s in self.step(f, i, states[i].copy()):
                if j is None or j >= n:
                    continue
                if states[j] is None:
                    states[j] = s
                    work.append(j)
                else:
                    nj = states[j].joined(s)
                    if nj is not None:
                        states[j] = nj
                        work.append(j)
        return list(self.findings.values()), frozenset(self.ret)

    def sink(self, f, addr, st, r, what):
        v = st.get(r)
        if is_t(v) and v[2] is not None:
            self.findings.setdefault((addr, r), Finding(f, addr, "STALE", what, v))

    def escape(self, f, addr, what, v):
        # The thread pointer is a value (an owner id), not an address anything
        # dereferences: storing or passing it is not an escape.
        if v[1] - {THREAD_POINTER}:
            self.findings.setdefault((addr, "escape"),
                                     Finding(f, addr, "ESCAPE", what, v))

    def step(self, f, i, st):
        addr, mnem, ops = f.insns[i]
        nxt = [(i + 1, st)]
        if mnem in TRAPS:
            return []
        if mnem == "ret":
            self.returned(f, addr, st)
            return []
        if mnem in ("b", "bl", "blr", "br") or COND_BRANCH_RE.match(mnem) or \
                mnem in TESTS:
            return self.branch(f, i, st)
        if mnem == "mrs":
            src = ("T", frozenset([THREAD_POINTER]), None) \
                if ops[1].upper().startswith("TPIDR") else None
            st.set(reg(ops[0]), src)
            return nxt
        if mnem in COMPARES:
            for o in ops:
                for r in regs_in(o):
                    self.sink(f, addr, st, r, "compared by %s" % mnem)
            return nxt
        if mnem in NO_DEST:
            return nxt
        if any(o.startswith("[") for o in ops):
            return self.memory(f, i, st)
        if mnem.startswith("ldr") and ops and ADDR_RE.match(ops[-1]):
            st.set(reg(ops[0]), None)            # pc-relative literal
            return nxt
        if not ops or (reg(ops[0]) is None and not ops[0].startswith("{")):
            raise ScanError("%s+0x%x: unmodelled instruction %s %s"
                            % (f.name, addr - f.start, mnem, ", ".join(ops)))
        dests = regs_in(ops[0]) if ops[0].startswith("{") else [reg(ops[0])]
        srcs = [r for o in ops[1:] for r in regs_in(o)]
        if mnem in READS_DEST:
            srcs += [d for d in dests if d]
        out = self.arith(mnem, ops, st, srcs)
        for d in dests:
            st.set(d, out)
        return nxt

    def arith(self, mnem, ops, st, srcs):
        vals = [st.get(r) for r in srcs]
        t = taint_of(vals)
        if t is not None:
            if mnem in ("madd", "msub", "smaddl", "umaddl"):
                t = taint_of(vals[2:3])          # only the addend is a pointer
            return t if mnem in PTR_OPS else None
        if mnem in ("adrp", "adr"):
            m = ADDR_RE.match(ops[1])
            return ("K", int(m.group(1), 16)) if m else None
        if mnem == "mov" and len(ops) == 2:
            return vals[0] if srcs else (("K", imm(ops[1])) if imm(ops[1]) is not None else None)
        if mnem in ("add", "sub") and len(ops) >= 3:
            a = vals[0] if srcs else None
            k = imm(ops[2])
            if k is not None:
                if len(ops) == 4 and "lsl #12" in ops[3]:
                    k <<= 12
                k = k if mnem == "add" else -k
                if a is not None and a[0] == "S":
                    return ("S", a[1] + k if a[1] is not None else None)
                if a is not None and a[0] == "K":
                    c = a[1] + k
                    return ("D", self.img.tlv[c]) if c in self.img.tlv else ("K", c)
                return None
            if mnem == "add" and len(vals) == 2 and all(vals):
                sh = 0
                if len(ops) == 4:
                    m = re.search(r"lsl #(\d+)", ops[3])
                    sh = int(m.group(1)) if m else 0
                for x, y in ((vals[0], vals[1]), (vals[1], vals[0])):
                    if x[0] == "K" and y[0] == "E":
                        return ("J", x[1], y[1], y[2], y[3], sh)
        return None

    # ---- memory ----
    def slot_of(self, st, m):
        if m.index is not None:
            return None
        b = st.get(m.base)
        if b is not None and b[0] == "S" and b[1] is not None:
            return b[1] + (0 if m.post is not None else m.off)
        return None

    def load_slot(self, st, key, size):
        """The slot at key, plus any taint in a slot the load overlaps."""
        v = st.slots.get(key)
        for k, sv in st.slots.items():
            if k != key and is_t(sv) and (k == "?" or (k < key + size and key < k + 8)):
                v = join(v, sv)
        return v

    def store_slot(self, st, key, size, val):
        for k in [k for k in st.slots if k != "?" and k < key + size and key < k + 8]:
            del st.slots[k]
        if val is not None and (size >= 8 or is_t(val)):
            st.slots[key] = val
            if size == 16:
                st.slots[key + 8] = val

    def memory(self, f, i, st):
        addr, mnem, ops = f.insns[i]
        mi = next(k for k, o in enumerate(ops) if o.startswith("["))
        post = imm(ops[mi + 1]) if len(ops) > mi + 1 else None
        m = Mem(ops[mi], post)
        self.sink(f, addr, st, m.base, "used as the %s address" % mnem)
        if m.index:
            self.sink(f, addr, st, m.index, "used as the %s index" % mnem)
        base = st.get(m.base)
        on_stack = base is not None and base[0] == "S"
        key = self.slot_of(st, m)
        data = ops[:mi]
        rmw = mnem.startswith(RMW_PREFIXES)
        exclusive_store = mnem.startswith(("stxr", "stlxr", "stxp", "stlxp"))

        if mnem.startswith("st") or rmw:
            if exclusive_store:
                stored = data[1:]
            elif mnem.startswith("cas"):
                stored = data[1:]
            elif mnem.startswith("ld") or mnem.startswith("swp"):
                stored = data[:1]
            else:
                stored = data
            for o in stored:
                for r in regs_in(o):
                    v = st.get(r)
                    if not is_t(v):
                        continue
                    if on_stack:
                        continue                 # a spill, followed below
                    if v[2] is not None:
                        self.sink(f, addr, st, r, "stored by %s" % mnem)
                    else:
                        self.escape(f, addr, "stored to memory by %s" % mnem, v)
            if key is not None and not rmw:
                pos = key
                for o in stored:
                    w = access_width(mnem, o)
                    rs = regs_in(o)
                    self.store_slot(st, pos, w, st.get(rs[0]) if rs else None)
                    pos += w
            elif on_stack:
                t = taint_of(st.get(r) for o in stored for r in regs_in(o))
                if t is not None:
                    st.slots["?"] = join(st.slots.get("?"), t)
            if exclusive_store:
                st.set(reg(data[0]), None)
            if rmw and not mnem.startswith("st"):
                written = data[1:2] if not mnem.startswith("cas") else data[:1]
                if mnem.startswith("casp"):
                    written = data[:2]
                for o in written:
                    for r in regs_in(o):
                        st.set(r, None)
        else:
            pos = key
            for o in data:
                w = access_width(mnem, o)
                rs = regs_in(o)
                if o.startswith("{"):
                    for r in rs:
                        st.set(r, taint_of(st.slots.values()) if on_stack else None)
                    continue
                r = rs[0] if rs else ""
                if base is not None and base[0] == "D":
                    if m.off != 0 or w != 8 or m.index:
                        raise ScanError("%s+0x%x: unmodelled TLV descriptor "
                                        "access" % (f.name, addr - f.start))
                    st.set(r, ("H", base[1]))
                elif base is not None and base[0] == "K" and m.index and \
                        mnem in ("ldrb", "ldrh", "ldrsw"):
                    st.set(r, ("E", base[1], w, mnem == "ldrsw"))
                elif base is not None and base[0] == "K" and m.index is None:
                    sym = self.img.data_syms.get(base[1] + m.off)
                    st.set(r, ("P", sym) if sym else None)
                elif pos is not None:
                    st.set(r, self.load_slot(st, pos, w))
                elif on_stack:
                    st.set(r, taint_of(st.slots.values()))
                else:
                    st.set(r, None)
                if pos is not None:
                    pos += w
        if m.writeback or m.post is not None:
            delta = m.off if m.writeback else m.post
            b = st.get(m.base)
            if b is not None and b[0] == "S":
                st.set(m.base, ("S", b[1] + delta if b[1] is not None else None))
            elif not is_t(b):
                st.set(m.base, None)
        return [(i + 1, st)]

    # ---- control flow ----
    def returned(self, f, addr, st):
        self.sink(f, addr, st, "x0", "returned")
        v = st.get("x0")
        if is_t(v):
            self.ret.update(v[1])

    def branch(self, f, i, st):
        addr, mnem, ops = f.insns[i]
        if mnem in TESTS:
            self.sink(f, addr, st, reg(ops[0]), "tested by %s" % mnem)
        kind, t = self.target(f, i)
        if kind is None and mnem in ("b", "bl") or COND_BRANCH_RE.match(mnem) \
                or mnem in TESTS:
            if kind is None:
                to = f.index.get(int(ADDR_RE.match(ops[-1]).group(1), 16))
                if to is None:
                    raise ScanError("%s+0x%x: branch into the middle of an "
                                    "instruction" % (f.name, addr - f.start))
                if mnem == "b":
                    return [(to, st)]
                return [(i + 1, st), (to, st.copy())]
        if kind == "indirect":
            target = st.get(reg(ops[0]))
            if mnem == "blr" and target is not None and target[0] == "H":
                if st.get("x0") != ("D", target[1]):
                    raise ScanError("%s+0x%x: TLV thunk call without its "
                                    "descriptor in x0" % (f.name, addr - f.start))
                self.site(f, addr, "tlv", None)
                st.set("x30", None)
                st.set("x0", ("T", frozenset([target[1]]), None))
                return [(i + 1, st)]
            if mnem == "br" and target is not None and target[0] == "J":
                return self.jump_table(f, i, target, st)
            t = ("ptr", target[1]) if target is not None and target[0] == "P" \
                else ("reg", None)
            self.site(f, addr, *t)
            if mnem == "br" and st.sp != 0:
                raise ScanError("%s+0x%x: indirect branch that is neither a "
                                "jump table nor a tail call" % (f.name, addr - f.start))
        if kind == "func" and t.outlined:
            return self.inline(f, i, t, st)
        tail = mnem in ("b", "br") or COND_BRANCH_RE.match(mnem) or mnem in TESTS
        after = self.call(f, i, st.copy() if tail else st, kind, t)
        if tail:
            v = after.get("x0")
            if is_t(v):
                self.ret.update(v[1])
            if mnem in ("b", "br"):
                return []
            return [(i + 1, st)]
        return [(i + 1, after)]

    def call(self, f, i, st, kind, t):
        addr = f.insns[i][0]
        if kind == "indirect":
            self.sink(f, addr, st, reg(f.insns[i][2][0]), "called through")
            parks = "indirect call" if self.site_may_park({t}) else None
            name = "*%s" % (t[1] or f.insns[i][2][0])
            t = {t}
        elif kind == "stub":
            parks, name = self.stub_may_park(t), t
        else:
            parks, name = self.may_park.get(t.start), t.name
        for r in sorted(self.callee_reads(kind, t)):
            v = st.get(r)
            if not is_t(v):
                continue
            if v[2] is not None:
                self.sink(f, addr, st, r, "passed in %s to %s" % (r, name.lstrip("_")))
            elif parks:
                self.escape(f, addr, "passed in %s to %s, which may park"
                            % (r, name), v)
            else:
                self.passed_fresh.append((f.name, addr - f.start, name, v[1]))
        out = OUT_POINTERS.get(t) if kind == "stub" else None
        x0 = st.get("x0")
        for r in CALLER_SAVED:
            st.regs.pop(r, None)
        if out and x0 is not None and x0[0] == "S" and x0[1] is not None:
            for off, name in out.items():
                self.store_slot(st, x0[1] + off, 8, ("P", name))
        if parks:
            for k, v in list(st.regs.items()):
                if is_t(v) and v[2] is None:
                    st.regs[k] = ("T", v[1], addr)
            for k, v in list(st.slots.items()):
                if is_t(v) and v[2] is None:
                    st.slots[k] = ("T", v[1], addr)
        if kind == "func" and t.start in self.returns_tls:
            st.set("x0", ("T", self.returns_tls[t.start], None))
        return st

    def jump_table(self, f, i, j, st):
        _, base, table, size, signed, sh = j
        n = self.table_bound(f, i)
        if n is None:
            raise ScanError("%s+0x%x: jump table without a bound check"
                            % (f.name, f.insns[i][0] - f.start))
        out = []
        for k in range(n):
            e = self.img.read(table + k * size, size, signed)
            to = f.index.get(base + (e << sh)) if e is not None else None
            if to is None:
                raise ScanError("%s: entry %d of the jump table at 0x%x does "
                                "not land in the function" % (f.name, k, table))
            out.append((to, st.copy()))
        return out

    def table_bound(self, f, i):
        for j in range(i - 1, max(-1, i - 32), -1):
            mnem = f.insns[j][1]
            if COND_BRANCH_RE.match(mnem) and mnem[2:] in ("hi", "hs", "cs"):
                for k in range(j - 1, max(-1, j - 4), -1):
                    _, m2, o2 = f.insns[k]
                    if m2 == "cmp" and len(o2) == 2 and imm(o2[1]) is not None:
                        return imm(o2[1]) + (1 if mnem.endswith("hi") else 0)
                return None
        return None

    def inline(self, f, i, ol, st):
        """An outlined helper shares its caller's frame and registers: run
        its straight-line body on the caller's state.  `bl` resumes after the
        call site; `b` (and a ret reached by it) leaves the caller."""
        addr, mnem, _ = f.insns[i]
        if mnem not in ("bl", "b"):
            raise ScanError("%s+0x%x: conditional branch into %s"
                            % (f.name, addr - f.start, ol.name))
        for k, (a2, m2, o2) in enumerate(ol.insns):
            if m2 == "ret":
                if mnem == "b":
                    self.returned(f, addr, st)
                    return []
                return [(i + 1, st)]
            if m2 in ("blr", "br") or COND_BRANCH_RE.match(m2) or m2 in TESTS:
                raise ScanError("%s has control flow at 0x%x" % (ol.name, a2))
            if m2 in ("b", "bl"):
                kind, t = self.target(ol, k)
                if kind == "func" and t.outlined:
                    raise ScanError("%s calls another outlined helper" % ol.name)
                tail = m2 == "b"
                after = self.call(f, i, st.copy() if tail else st, kind, t)
                if tail:
                    v = after.get("x0")
                    if mnem == "b":
                        if is_t(v):
                            self.ret.update(v[1])
                        return []
                    return [(i + 1, after)]
                st = after
                continue
            fake = Func(f.name, f.start)
            fake.insns, fake.index = [(addr, m2, o2)], {addr: 0}
            res = self.step(fake, 0, st)
            st = res[0][1] if res else st
        raise ScanError("%s falls off its end" % ol.name)


def default_extension():
    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.normpath(os.path.join(here, "..", "..", "src"))
    hits = sorted(glob.glob(os.path.join(src, "stackweave_c*.so")))
    return hits[0] if hits else None


def main(argv):
    verbose = "-v" in argv
    args = [a for a in argv[1:] if a != "-v"]
    so = args[0] if args else default_extension()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        print("check_tls_after_park: only arm64 macOS is supported (got %s/%s)"
              % (sys.platform, platform.machine()))
        return 2
    if not so or not os.path.exists(so):
        print("check_tls_after_park: no extension to check (build it, or pass "
              "its path)")
        return 2
    with tempfile.TemporaryDirectory() as tmp:
        try:
            return check(so, thin_arm64(so, tmp), verbose)
        except ScanError as e:
            print("check_tls_after_park: cannot vouch for %s: %s" % (so, e))
            return 2


def check(so, image_path, verbose):
    img = Image(image_path)
    if not img.tlv:
        raise ScanError("no __thread_vars descriptors")
    an = Analyzer(img)
    an.classify_sites()
    an.build_call_graph()
    an.compute_arg_reads()

    findings, analyzed, skipped = {}, set(), set()
    changed = True
    while changed:                       # until returns_tls is stable
        changed = False
        an.passed_fresh = []
        for f in img.funcs:
            if f.outlined or not an.has_source(f):
                continue
            fs, rv = an.analyze(f)
            analyzed.add(f.name)
            if f.name in NATIVE_ONLY:
                skipped.add(f.name)
            else:
                findings[f.start] = fs
            if rv and rv != an.returns_tls.get(f.start):
                an.returns_tls[f.start] = rv
                changed = True

    missing = ANCHORS - analyzed
    if missing:
        raise ScanError("found no TLS access in %s" % ", ".join(sorted(missing)))
    every = sorted((x for fs in findings.values() for x in fs), key=lambda x: x.addr)
    accepted = [x for x in every if x.kind == "STALE" and x.func.name in ACCEPTED
                and x.vars <= ACCEPTED[x.func.name][0]]
    bad = [x for x in every if x not in accepted]
    print(so)
    print("  %d functions take a TLS address or the thread pointer; %d of %d "
          "functions may park" % (len(analyzed), sum(1 for v in an.may_park.values() if v),
                                  len(img.funcs)))
    if verbose and an.returns_tls:
        print("  x0 holds a TLS address at return (callers treat it as one): %s"
              % ", ".join(sorted(img.by_addr[s].name.lstrip("_")
                                 for s in an.returns_tls)))
    if verbose:
        for u in an.unmodelled:
            print("  note: no TLS in %s; its indirect calls count as may-park" % u)
    names = {f.name for f in img.funcs}
    for name in sorted((set(NATIVE_ONLY) | set(ACCEPTED)) - names):
        print("  note: %s is listed but not in the image" % name)
    if skipped:
        print("  skipped (NATIVE_ONLY): %s" % ", ".join(
            sorted(s.lstrip("_") for s in skipped)))
    for name in sorted({x.func.name for x in accepted}):
        n = sum(1 for x in accepted if x.func.name == name)
        print("  accepted: %d stale use(s) in %s -- %s"
              % (n, name.lstrip("_"), ACCEPTED[name][1]))
    for name in sorted(set(ACCEPTED) & names - {x.func.name for x in accepted}):
        print("  note: ACCEPTED %s matched nothing in this build" % name)
    if verbose:
        for fn, off, callee, tv in an.passed_fresh:
            print("  note: %s+0x%x passes a TLS address [%s] to %s, which "
                  "cannot park" % (fn, off, ", ".join(sorted(tv)), callee))
    if bad:
        for x in bad:
            print("UNSAFE: %s" % x)
            if verbose and x.park is not None:
                print("        the call: %s" % an.describe_call(x.func, x.park))
        print("UNSAFE: %d use(s) of a TLS address after a call that can park "
              "the fiber, or escapes the check cannot follow -- see "
              "docs/dev/TSAN.md, finding A2" % len(bad))
        return 1
    print("OK: no TLS address is used after a call that can park the fiber")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

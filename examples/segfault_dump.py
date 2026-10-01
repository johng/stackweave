"""Catching a segfault -- classified crash dumps for a fiber stack overflow.

Every fiber runs on its own fixed C stack with a guard page below it.  Most
deep recursion never reaches that page: CPython 3.14's overflow check is
pointed at each fiber's stack, so runaway Python recursion and recursion in
stdlib C code (``json``, ``pickle``, ``repr``) raises a catchable
``RecursionError`` instead.  Native code that recurses without consulting that
check, such as a third-party C extension, can still run off the end of the
stack.  That hits the guard page and the process dies with a
bare ``Segmentation fault`` (``Bus error`` on macOS), with no clue which fiber
or why.

``stackweave.inspect.install_crash_handler()`` installs a fatal-signal handler
that turns it into a *classified* dump: it names the overflowing fiber and its
stack size and tells you what to do about it.
The fault is unrecoverable -- a SIGSEGV (SIGBUS on macOS) can't be turned into a
catchable Python exception, so the process still dies -- but now it dies *informatively*, instead
of leaving you staring at a bare "Segmentation fault".

In your own program you just call ``install_crash_handler()`` once at startup.
Here we run the doomed fiber in a CHILD process so we can show you the dump
it produces and then exit cleanly.

Run:
    python3 examples/segfault_dump.py
"""
import signal
import subprocess
import sys
import textwrap

# What the child does: install the handler, then overflow a fiber's stack with
# native recursion that bypasses CPython's overflow check.
CHILD = textwrap.dedent("""
    import faulthandler
    import stackweave
    import stackweave_c

    stackweave.inspect.install_crash_handler()      # classify fatal signals

    def recurse_in_c():
        # CPython's own test helper: C recursion with a 4 KiB frame per level
        # and no RecursionError check, standing in for a C extension that
        # recurses without one.  It runs off the end of any fiber stack.
        faulthandler._stack_overflow()

    stackweave_c.fiber(recurse_in_c)
    stackweave_c.run()
""")


def main():
    print("Spawning a fiber that overflows its C stack on purpose...\n")
    proc = subprocess.run([sys.executable, "-c", CHILD],
                          capture_output=True, text=True)

    # The classified crash dump the handler wrote to stderr (look for the
    # ">>> GOROUTINE STACK OVERFLOW <<<" line naming the fiber + its size).
    sys.stdout.write(proc.stderr)

    sig = -proc.returncode if proc.returncode < 0 else None
    if sig is not None:
        print("\n[child died from {0} -- but now you know exactly which "
              "fiber and why]".format(signal.Signals(sig).name))
    else:
        # Some platforms / sanitizer builds report it differently; the dump
        # above is the point.
        print("\n[child exit code: {0}]".format(proc.returncode))


if __name__ == "__main__":
    main()

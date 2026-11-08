# monkey.patch's os.read/os.write switch a pollable fd to O_NONBLOCK and never
# switch it back.  The flag lives on the open file description, which other fds
# and other code (sys.stdout's FileIO, a Popen pipe read by a thread) share.
from prelude import *
import stackweave.monkey as monkey; monkey.patch()
import subprocess
def main():
    r, w = os.pipe()
    os.write(w, b"x")
    print("before: blocking(r)=%s" % os.get_blocking(r), flush=True)
    os.read(r, 1)                      # cooperative read inside a fiber
    print("after:  blocking(r)=%s" % os.get_blocking(r), flush=True)
    # A Popen pipe: one patched os.read, then plain file iteration.
    p = subprocess.Popen([sys.executable, "-c",
        "import time,sys\nfor i in range(3):\n    print(i, flush=True); time.sleep(0.2)"],
        stdout=subprocess.PIPE)
    fd = p.stdout.fileno()
    first = os.read(fd, 2)             # patched: leaves the pipe non-blocking
    rest = []
    try:
        for line in p.stdout:          # BufferedReader over a now non-blocking fd
            rest.append(line)
    except Exception as e:
        rest.append(repr(e))
    p.wait()
    print("first=%r rest=%r" % (first, rest), flush=True)
    print("PASS" if os.get_blocking(r) and len(rest) == 2 else "FAIL", flush=True)
stackweave.run(2, main)

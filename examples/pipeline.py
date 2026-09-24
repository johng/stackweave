"""Pipeline — stages connected by channels.

Each stage is a fiber that reads from an input channel, does one
job, and writes to an output channel; closing an output propagates the
"done" signal downstream.  Here: generate 1..N -> square -> sum.

Run:
    python3 examples/pipeline.py
"""

import os

import stackweave

# Free-threaded build: fan fibers across all cores (M:N scheduler).
HUBS = os.cpu_count() or 4

def generate(out, n):
    for i in range(1, n + 1):
        out.send(i)
    out.close()

def square(inp, out):
    for v in inp:
        out.send(v * v)
    out.close()

def sum_all(inp, result):
    total = 0
    for v in inp:
        total += v
    result.send(total)

def main():
    nums = stackweave.Chan(10)
    squares = stackweave.Chan(10)
    result = stackweave.Chan(1)

    stackweave.fiber(generate, nums, 10)
    stackweave.fiber(square, nums, squares)
    stackweave.fiber(sum_all, squares, result)

    print("sum of squares 1..10 =", result.recv()[0])   # 385

if __name__ == "__main__":
    stackweave.run(HUBS, main)

import stackweave_c, gc, sys
def rss():
    with open('/proc/self/status') as f:
        for l in f:
            if l.startswith('VmRSS'): return int(l.split()[1])
def noop(): pass
def cycle(nfib):
    stackweave_c.mn_init(4)
    for _ in range(nfib): stackweave_c.mn_fiber(noop)
    stackweave_c.mn_run(); stackweave_c.mn_fini()
mode = sys.argv[1]
if mode == 'zero':
    for _ in range(20): cycle(0)
    gc.collect(); r0 = rss()
    for _ in range(300): cycle(0)
    gc.collect(); r1 = rss()
elif mode == 'one':
    for _ in range(20): cycle(1)
    gc.collect(); r0 = rss()
    for _ in range(300): cycle(1)
    gc.collect(); r1 = rss()
elif mode == 'highlevel':
    import stackweave
    for _ in range(20): stackweave.run(4, noop)
    gc.collect(); r0 = rss()
    for _ in range(300): stackweave.run(4, noop)
    gc.collect(); r1 = rss()
print('%s: rss %d->%d kB, %.2f kB/cycle' % (mode, r0, r1, (r1-r0)/300.0))

import stackweave_c, gc
def rss():
    with open('/proc/self/status') as f:
        for l in f:
            if l.startswith('VmRSS'): return int(l.split()[1])
def noop(): pass
def cycle():
    stackweave_c.mn_init(4)
    for _ in range(10): stackweave_c.mn_fiber(noop)
    stackweave_c.mn_run(); stackweave_c.mn_fini()
for _ in range(20): cycle()
gc.collect(); r = [rss()]
for phase in range(4):
    for _ in range(200): cycle()
    gc.collect(); r.append(rss())
print('rss checkpoints:', r)
print('per-phase growth kB/cycle:', [(r[i+1]-r[i])/200.0 for i in range(4)])

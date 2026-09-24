import sys, stackweave_c
which = sys.argv[1]
if which == 'subclass':
    try:
        class MyChan(stackweave_c.Chan):
            def __init__(self): pass
        print('subclass created ok')
        MyChan().recv()
    except TypeError as e:
        print('TypeError:', e)
elif which == 'mutex':
    m = stackweave_c.Mutex.__new__(stackweave_c.Mutex)
    sys.stdout.write('mutex created\n'); sys.stdout.flush()
    m.locked()
    print('survived locked()')
elif which == 'select':
    ch = stackweave_c.Chan.__new__(stackweave_c.Chan)
    sys.stdout.write('chan created\n'); sys.stdout.flush()
    stackweave_c.select([(ch, None)])
    print('survived select')

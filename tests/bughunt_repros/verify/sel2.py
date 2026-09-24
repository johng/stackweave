import sys, stackweave_c
ch = stackweave_c.Chan.__new__(stackweave_c.Chan)
sys.stdout.write('chan created\n'); sys.stdout.flush()
stackweave_c.select([('recv', ch)])
print('survived select')

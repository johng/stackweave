import stackweave_c
ch = stackweave_c.Chan.__new__(stackweave_c.Chan)
print('created', ch)
ch.send(1)   # NULL runloom_chan_t* -> SIGSEGV
print('survived')

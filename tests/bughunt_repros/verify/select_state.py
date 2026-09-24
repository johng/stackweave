import stackweave, stackweave_c as rc
ch, ch2 = rc.Chan(0), rc.Chan(0)
def main():
    rc.mn_fiber(lambda: ch2.recv())
    rc.mn_fiber(lambda: rc.select([('recv', ch)]))
    stackweave.sleep(0.2)
    for f in stackweave.fibers():
        print(f['id'], f.get('state'), f.get('wait_reason'))
    ch.send(1); ch2.send(1)
stackweave.run(2, main)

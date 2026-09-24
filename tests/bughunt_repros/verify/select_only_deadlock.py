import stackweave, stackweave_c as rc
ch = rc.Chan(0)
def main():
    rc.mn_fiber(lambda: rc.select([('recv', ch)]))
stackweave.run(2, main)

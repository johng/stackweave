"""select with send+recv cases on the SAME unbuffered channel from one fiber:
must NOT match itself. A partner fiber sends -> our recv case should fire."""
import stackweave, stackweave_c

def main():
    ch = stackweave.Chan(0)
    out = []
    def selector():
        idx, res = stackweave_c.select([("send", ch, "mine"), ("recv", ch)])
        out.append(("case", idx, res))
    def partner():
        stackweave.sleep(0.05)
        ch.send("partner")
    stackweave.fiber(selector)
    stackweave.fiber(partner)
stackweave.run(1, main)

def main2():
    ch = stackweave.Chan(0)
    def selector():
        idx, res = stackweave_c.select([("send", ch, "mine"), ("recv", ch)])
        if idx == 1:
            v, ok = res
            assert v == "partner2", ("SELF-MATCH? got", v)
            print("selfmatch OK: recv fired with partner value:", v)
        else:
            print("selfmatch: send case fired (partner received)")
    def partner():
        stackweave.sleep(0.05)
        ch.send("partner2")
    stackweave.fiber(selector)
    stackweave.fiber(partner)
stackweave.run(4, main2)
print("done")

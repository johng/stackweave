import socket, ssl, os, sys, time
import stackweave, stackweave_c
D = os.path.dirname(os.path.abspath(__file__))
MODE = sys.argv[1]  # plain | tls

def main():
    a, b = socket.socketpair(); state = {}
    if MODE == "plain":
        def reader():
            try: state["out"] = ("recv", b.recv(100))
            except Exception as e: state["out"] = ("exc", type(e).__name__, str(e))
        stackweave.fiber(reader)
    else:
        sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        sctx.load_cert_chain(D+"/cert.pem", D+"/key.pem")
        cctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        cctx.check_hostname = False; cctx.verify_mode = ssl.CERT_NONE
        stackweave.fiber(lambda: sctx.wrap_socket(a, server_side=True))
        ctls = cctx.wrap_socket(b)
        def reader():
            try: state["out"] = ("recv", ctls.recv(100))
            except Exception as e: state["out"] = ("exc", type(e).__name__, str(e))
        stackweave.fiber(reader)
    stackweave.sleep(0.3)
    print("cancelled", stackweave_c.cancel_all_parked(), flush=True)
    stackweave.sleep(0.5)
    print("reader:", state.get("out", "STILL PARKED"), flush=True)

stackweave.monkey.patch(); stackweave.run(2, main)
print("run() returned", flush=True)

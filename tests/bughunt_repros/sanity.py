import stackweave, socket
def main():
    stackweave.monkey.patch()
    def fib():
        a, b = socket.socketpair()
        a.sendall(b"hi")
        print("got:", b.recv(10))
        a.close(); b.close()
    stackweave.fiber(fib)
stackweave.run(2, main)
print("OK")

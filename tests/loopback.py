"""Paced rtl_tcp byte stream for the real live receiver; no SDR or RF hardware."""
import socket
import struct
import threading
import time
from sources import read_exact


class BenchSocket:
    def __init__(self, path):
        self.path=path
        self.listener=socket.socket()
        self.listener.bind(("127.0.0.1",0))
        self.listener.listen(1)
        self.listener.settimeout(10)
        self.address=f"127.0.0.1:{self.listener.getsockname()[1]}"
        self.commands=[]
        self.error=None
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self.serve,daemon=True)

    def serve(self):
        try:
            with self.listener:
                conn,_=self.listener.accept()
                with conn,self.path.open("rb") as iq:
                    conn.settimeout(5.)
                    header=b"RTL0"+struct.pack("!II",5,29)
                    conn.sendall(header[:3])
                    conn.sendall(header[3:])
                    self.commands=[struct.unpack("!BI",read_exact(conn,5,self.stop)) for _ in range(6)]
                    # Match tuning settle time in the actual live entry point.
                    conn.sendall(bytes([127])*500000)
                    started=time.monotonic()
                    sent=0
                    while not self.stop.is_set():
                        data=iq.read(65521)  # odd byte counts cut I/Q pairs across TCP receives
                        if not data:
                            break
                        sent+=len(data)
                        self.stop.wait(max(0.,started+sent/2000000-time.monotonic()))
                        conn.sendall(data)
        except Exception as exc:
            self.error=exc

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self,*args):
        self.stop.set()
        self.thread.join(10)
        if self.thread.is_alive():
            raise RuntimeError("Loopback test server did not stop")
        if self.error and not args[0]:
            raise self.error

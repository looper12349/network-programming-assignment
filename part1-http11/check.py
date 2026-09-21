#!/usr/bin/env python3
"""check.py - the marking script, written out so I fail it before you do.

    one socket. every request.

    s = socket.create_connection(("localhost", 8080))

Run it twice over the same connection: once request-by-request, once with
all six written in a single send() - that second pass is pipelining, and it
only works if the server answers in order and never reads past the end of
a request.

    python3 check.py                 # against a server already on :8080
    python3 check.py --spawn         # start one on a free port, then check
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

CASES: List[Tuple[str, str, int, Optional[str]]] = [
    # method, target, expected status, expected body (None = do not care)
    ("GET", "/add?a=2&b=3", 200, "5"),
    ("GET", "/sub?a=10&b=4", 200, "6"),
    ("GET", "/mul?a=6&b=7", 200, "42"),
    ("GET", "/div?a=9&b=3", 200, "3"),
    ("GET", "/div?a=1&b=0", 400, None),
    ("GET", "/add?a=x&b=3", 400, None),
    ("GET", "/pow?a=2&b=8", 404, None),
    ("POST", "/add", 405, None),
]

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


class Reader:
    """Response parser that consumes exactly Content-Length bytes."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = bytearray()
        self.responses = 0

    def _fill(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("server closed the connection")
        self.buf += chunk

    def read_response(self) -> Tuple[int, dict, bytes]:
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        self.buf = bytearray(rest)

        lines = head.split(b"\r\n")
        status = int(lines[0].split(b" ")[1])
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            headers[name.decode().lower()] = value.strip().decode()

        if status == 100:  # interim response, keep reading
            return self.read_response()

        length = int(headers.get("content-length", 0))
        while len(self.buf) < length:
            self._fill()
        body = bytes(self.buf[:length])
        del self.buf[:length]
        self.responses += 1
        return status, headers, body


def request_bytes(method: str, target: str, host: str) -> bytes:
    return (f"{method} {target} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: check.py\r\n"
            f"\r\n").encode()


def still_open(sock: socket.socket) -> bool:
    """True if the peer has not sent FIN and the socket is usable."""
    sock.setblocking(False)
    try:
        data = sock.recv(1, socket.MSG_PEEK)
        return data != b""
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        try:
            sock.setblocking(True)
        except OSError:
            pass


def run_pass(host: str, port: int, *, pipelined: bool) -> bool:
    label = "pipelined (one send, six answers in order)" if pipelined else "sequential"
    print(f"\n{BOLD}--- {label} ---{OFF}")

    t0 = time.time()
    sock = socket.create_connection((host, port), timeout=5)
    handshakes = 1
    reader = Reader(sock)
    authority = f"{host}:{port}"
    ok = True

    if pipelined:
        blob = b"".join(request_bytes(m, t, authority) for m, t, _, _ in CASES)
        sock.sendall(blob)

    for method, target, want_status, want_body in CASES:
        if not pipelined:
            sock.sendall(request_bytes(method, target, authority))
        status, headers, body = reader.read_response()
        text = body.decode("utf-8", "replace").strip()
        good = status == want_status and (want_body is None or text == want_body)
        ok = ok and good
        mark = f"{GREEN}ok{OFF}" if good else f"{RED}FAIL{OFF}"
        shown = text.splitlines()[0] if text else ""
        print(f"  {mark:<14} {method:<4} {target:<16} -> {status:<3} {DIM}{shown}{OFF}")

    open_after = still_open(sock)
    print(f"\n  socket still open: {GREEN if open_after else RED}{open_after}{OFF}")
    print(f"  {handshakes} TCP handshake, {reader.responses} responses "
          f"{DIM}({time.time() - t0:.3f}s){OFF}")

    # A seventh request proves the connection is not merely un-FIN'd but live.
    if open_after:
        sock.sendall(request_bytes("GET", "/mul?a=7&b=6", authority))
        status, _, body = reader.read_response()
        alive = status == 200 and body.strip() == b"42"
        print(f"  reuse after the marking set: "
              f"{GREEN if alive else RED}{status} {body.decode().strip()}{OFF}")
        ok = ok and alive
    else:
        ok = False

    sock.close()
    return ok


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--spawn", action="store_true",
                        help="start server11.py on the given port first")
    args = parser.parse_args(argv)

    proc = None
    if args.spawn:
        script = Path(__file__).with_name("server11.py")
        proc = subprocess.Popen(
            [sys.executable, str(script), "--port", str(args.port), "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection((args.host, args.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)

    try:
        ok = run_pass(args.host, args.port, pipelined=False)
        ok = run_pass(args.host, args.port, pipelined=True) and ok
    except (ConnectionError, socket.timeout) as exc:
        print(f"\n{RED}connection died: {exc}{OFF}")
        ok = False
    finally:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=5)

    print(f"\n{BOLD}{GREEN + 'PASS' if ok else RED + 'FAIL'}{OFF}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

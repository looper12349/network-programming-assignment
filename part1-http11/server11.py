#!/usr/bin/env python3
"""server11.py - a calculator that stays on the line.

HTTP/1.1 on a raw TCP socket. Standard library only: no framework, no
http.server, no socketserver. One thread per connection, many requests
per connection.

    python3 server11.py --port 8080

The arithmetic is four lines. The whole exercise is the sentence below it:

    a request ends where Content-Length says it ends, and byte n+1
    belongs to the next request.

So the connection owns a buffer, not the request parser. Every read goes
through ByteStream, which hands back exactly what was asked for and keeps
the remainder for whoever comes next. Nothing in this file ever calls
recv() and assumes it got one request.
"""

from __future__ import annotations

import argparse
import re
import signal
import socket
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

SERVER_NAME = "server11/1.0"

# Limits. Every one of these exists so that a peer cannot make us allocate
# or wait for an amount it chooses.
MAX_REQUEST_LINE = 8 * 1024
MAX_HEAD_BYTES = 16 * 1024        # request line + all headers
MAX_HEADER_COUNT = 64
MAX_BODY_BYTES = 1024 * 1024      # 1 MiB
MAX_OPERAND = 10 ** 18            # bounded work per request

DEFAULT_IDLE_TIMEOUT = 15.0       # silence between requests before we hang up
DEFAULT_REQUEST_TIMEOUT = 10.0    # silence in the middle of a request
DEFAULT_MAX_REQUESTS = 100        # requests per connection before we hang up

REASONS = {
    100: "Continue",
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    411: "Length Required",
    414: "URI Too Long",
    413: "Content Too Large",
    417: "Expectation Failed",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    505: "HTTP Version Not Supported",
}

INT_RE = re.compile(rb"[+-]?[0-9]+\Z")
TOKEN_RE = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class ClientGone(Exception):
    """The peer closed or reset the connection."""


class IdleTimeout(Exception):
    """No new request arrived within the idle window."""


class RequestTimeout(Exception):
    """A request started and then stalled halfway through."""


class HttpError(Exception):
    """A response we can describe with a status code.

    `fatal` is the whole distinction that keeps the connection alive. If we
    still know where this request ends, the error is a semantic one: answer
    it and stay on the line. If the framing itself is in doubt - a bad
    request line, a Content-Length we cannot trust - we no longer know where
    the next request starts, so we answer once and hang up. Guessing is how
    you get request smuggling.
    """

    def __init__(self, status: int, detail: str, *, fatal: bool = False,
                 headers: Optional[List[Tuple[str, str]]] = None) -> None:
        super().__init__(f"{status} {detail}")
        self.status = status
        self.detail = detail
        self.fatal = fatal
        self.headers = headers or []


# --------------------------------------------------------------------------
# the buffer that makes persistence possible
# --------------------------------------------------------------------------


class ByteStream:
    """A socket plus the bytes we have already pulled off it.

    recv() returns whatever the kernel happens to hold, which is never
    "one request". It can be half a request line, or two whole requests and
    the first byte of a third. Both cases are normal and both are handled
    here, so the parser above never sees them.
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = bytearray()
        self.bytes_in = 0

    def _fill(self) -> None:
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            raise RequestTimeout()
        except ConnectionResetError:
            raise ClientGone()
        if not chunk:
            raise ClientGone()
        self.bytes_in += len(chunk)
        self.buf += chunk

    def buffered(self) -> bool:
        return bool(self.buf)

    def wait_for_first_byte(self, timeout: float) -> None:
        """Block until a new request begins, or give up after `timeout`.

        Called only between requests, never inside one. That is deliberate:
        an idle timeout you can defend is one that can never fire while the
        peer is mid-sentence.
        """
        if self.buf:
            return
        self.sock.settimeout(timeout)
        try:
            self._fill()
        except RequestTimeout:
            raise IdleTimeout()

    def read_until(self, delim: bytes, limit: int, too_big: HttpError) -> bytes:
        """Read up to and including `delim`; return what came before it."""
        search_from = 0
        while True:
            idx = self.buf.find(delim, search_from)
            if idx != -1:
                out = bytes(self.buf[:idx])
                del self.buf[: idx + len(delim)]
                return out
            if len(self.buf) > limit:
                raise too_big
            # a delimiter can straddle two recv() calls, so step back
            search_from = max(0, len(self.buf) - len(delim) + 1)
            self._fill()

    def read_exactly(self, n: int) -> bytes:
        """Return exactly n bytes. Byte n+1 stays in the buffer."""
        while len(self.buf) < n:
            self._fill()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


# --------------------------------------------------------------------------
# request
# --------------------------------------------------------------------------


class Request:
    def __init__(self, method: str, target: str, version: str,
                 headers: List[Tuple[str, str]], body: bytes) -> None:
        self.method = method
        self.target = target
        self.version = version
        self.headers = headers
        self.body = body
        self._map: Dict[str, List[str]] = {}
        for name, value in headers:
            self._map.setdefault(name.lower(), []).append(value)
        self.path, _, self.query = target.partition("?")

    def get(self, name: str) -> Optional[str]:
        values = self._map.get(name.lower())
        return values[0] if values else None

    def all(self, name: str) -> List[str]:
        return self._map.get(name.lower(), [])

    def has_token(self, header: str, token: str) -> bool:
        for value in self.all(header):
            for part in value.split(","):
                if part.strip().lower() == token:
                    return True
        return False


class Response:
    def __init__(self, status: int, body: bytes = b"",
                 content_type: str = "text/plain; charset=utf-8",
                 headers: Optional[List[Tuple[str, str]]] = None) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or []


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def parse_head(stream: ByteStream) -> Tuple[str, str, str, List[Tuple[str, str]]]:
    head = stream.read_until(
        b"\r\n\r\n", MAX_HEAD_BYTES,
        HttpError(431, "header block too large", fatal=True),
    )
    # RFC 9112 says a server SHOULD ignore at least one empty line before
    # the request line; older clients leave a stray CRLF after a body.
    while head.startswith(b"\r\n"):
        head = head[2:]

    lines = head.split(b"\r\n")
    request_line = lines[0]
    if len(request_line) > MAX_REQUEST_LINE:
        raise HttpError(414, "request line too long", fatal=True)

    parts = request_line.split(b" ")
    if len(parts) != 3:
        raise HttpError(400, "malformed request line", fatal=True)
    raw_method, raw_target, raw_version = parts

    if not TOKEN_RE.match(raw_method):
        raise HttpError(400, "method is not a token", fatal=True)
    if not raw_target.startswith(b"/"):
        # absolute-form and authority-form are legal HTTP but this server
        # only ever gets origin-form, so refuse rather than half-support it.
        raise HttpError(400, "target must be in origin-form", fatal=True)
    if raw_version not in (b"HTTP/1.1", b"HTTP/1.0"):
        if raw_version.startswith(b"HTTP/"):
            raise HttpError(505, "only HTTP/1.0 and HTTP/1.1 are spoken", fatal=True)
        raise HttpError(400, "malformed version", fatal=True)

    headers: List[Tuple[str, str]] = []
    for raw in lines[1:]:
        if not raw:
            continue
        if raw[:1] in (b" ", b"\t"):
            # obs-fold. Deleted from HTTP by RFC 7230 and a smuggling vector.
            raise HttpError(400, "obsolete line folding", fatal=True)
        if len(headers) >= MAX_HEADER_COUNT:
            raise HttpError(431, "too many header fields", fatal=True)
        name, sep, value = raw.partition(b":")
        if not sep or not TOKEN_RE.match(name):
            raise HttpError(400, "malformed header field", fatal=True)
        headers.append((name.decode("latin-1"), value.strip(b" \t").decode("latin-1")))

    return (raw_method.decode("latin-1"), raw_target.decode("latin-1"),
            raw_version.decode("latin-1"), headers)


def body_length(method: str, version: str,
                headers: List[Tuple[str, str]]) -> Tuple[str, int]:
    """Decide how this body is framed, before reading a byte of it.

    Returns ("none", 0) | ("length", n) | ("chunked", 0).
    """
    lengths: List[str] = []
    encodings: List[str] = []
    for name, value in headers:
        lname = name.lower()
        if lname == "content-length":
            lengths.append(value.strip())
        elif lname == "transfer-encoding":
            encodings.extend(p.strip().lower() for p in value.split(",") if p.strip())

    if encodings and lengths:
        # Two answers to "where does this end" is no answer at all.
        raise HttpError(400, "both Transfer-Encoding and Content-Length", fatal=True)

    if encodings:
        if version == "HTTP/1.0":
            raise HttpError(400, "Transfer-Encoding on an HTTP/1.0 request", fatal=True)
        if encodings[-1] != "chunked":
            raise HttpError(400, "final transfer coding must be chunked", fatal=True)
        if len(encodings) > 1:
            raise HttpError(501, "transfer coding not implemented", fatal=True)
        return ("chunked", 0)

    if lengths:
        if len(set(lengths)) > 1:
            raise HttpError(400, "conflicting Content-Length values", fatal=True)
        raw = lengths[0]
        if not raw.isdigit():
            raise HttpError(400, "Content-Length is not a number", fatal=True)
        n = int(raw)
        if n > MAX_BODY_BYTES:
            raise HttpError(413, "body too large", fatal=True)
        return ("length", n)

    return ("none", 0)


def read_body(stream: ByteStream, framing: str, length: int) -> bytes:
    if framing == "none":
        return b""
    if framing == "length":
        return stream.read_exactly(length)
    raise HttpError(501, "chunked bodies not implemented", fatal=True)


# --------------------------------------------------------------------------
# the calculator
# --------------------------------------------------------------------------


def percent_decode(raw: bytes) -> bytes:
    if b"%" not in raw and b"+" not in raw:
        return raw
    out = bytearray()
    i = 0
    raw = raw.replace(b"+", b" ")
    while i < len(raw):
        if raw[i : i + 1] == b"%":
            hexpair = raw[i + 1 : i + 3]
            if len(hexpair) != 2:
                raise HttpError(400, "truncated percent-escape in query")
            try:
                out.append(int(hexpair, 16))
            except ValueError:
                raise HttpError(400, "bad percent-escape in query")
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return bytes(out)


def parse_query(query: str) -> Dict[str, str]:
    params: Dict[str, str] = {}
    for pair in query.encode("latin-1").split(b"&"):
        if not pair:
            continue
        name, sep, value = pair.partition(b"=")
        if not sep:
            raise HttpError(400, "query parameter without a value")
        key = percent_decode(name).decode("utf-8", "replace")
        if key in params:
            raise HttpError(400, f"duplicate query parameter: {key}")
        params[key] = percent_decode(value).decode("utf-8", "replace")
    return params


def operand(params: Dict[str, str], name: str) -> int:
    if name not in params:
        raise HttpError(400, f"missing query parameter: {name}")
    raw = params[name].encode("utf-8")
    if not INT_RE.match(raw):
        raise HttpError(400, f"{name} is not an integer: {params[name]!r}")
    value = int(raw)
    if abs(value) > MAX_OPERAND:
        raise HttpError(400, f"{name} is out of range")
    return value


def format_number(value) -> str:
    if isinstance(value, int):
        return str(value)
    if value == int(value):
        return str(int(value))
    return f"{value:.10g}"


OPERATIONS: Dict[str, Callable[[int, int], object]] = {
    "/add": lambda a, b: a + b,
    "/sub": lambda a, b: a - b,
    "/mul": lambda a, b: a * b,
    "/div": lambda a, b: a // b if b and a % b == 0 else a / b,
}


def handle(request: Request) -> Response:
    if request.path not in OPERATIONS:
        raise HttpError(404, f"no such operation: {request.path}")

    # Path first, then method: an unknown path has no method to allow.
    if request.method not in ("GET", "HEAD"):
        raise HttpError(405, f"{request.method} is not allowed here",
                        headers=[("Allow", "GET, HEAD")])

    params = parse_query(request.query)
    unknown = sorted(set(params) - {"a", "b"})
    if unknown:
        raise HttpError(400, f"unknown query parameter: {unknown[0]}")

    a = operand(params, "a")
    b = operand(params, "b")

    if request.path == "/div" and b == 0:
        raise HttpError(400, "division by zero")

    result = OPERATIONS[request.path](a, b)
    return Response(200, (format_number(result) + "\n").encode("ascii"))


# --------------------------------------------------------------------------
# response writing
# --------------------------------------------------------------------------


def http_date(when: Optional[float] = None) -> str:
    t = time.gmtime(when if when is not None else time.time())
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return "%s, %02d %s %04d %02d:%02d:%02d GMT" % (
        days[t.tm_wday], t.tm_mday, months[t.tm_mon - 1], t.tm_year,
        t.tm_hour, t.tm_min, t.tm_sec,
    )


def error_response(err: HttpError) -> Response:
    body = f"{err.status} {REASONS.get(err.status, 'Error')}: {err.detail}\n"
    return Response(err.status, body.encode("utf-8"), headers=list(err.headers))


def serialize(response: Response, *, head_only: bool, close: bool,
              idle_timeout: float, remaining: int) -> bytes:
    reason = REASONS.get(response.status, "Unknown")
    out = bytearray(f"HTTP/1.1 {response.status} {reason}\r\n".encode("latin-1"))

    fields: List[Tuple[str, str]] = [
        ("Date", http_date()),
        ("Server", SERVER_NAME),
        ("Content-Type", response.content_type),
        # Always framed by length, never by "the connection ended". That is
        # the whole point of the exercise, and it applies to errors too.
        ("Content-Length", str(len(response.body))),
    ]
    fields.extend(response.headers)
    if close:
        fields.append(("Connection", "close"))
    else:
        fields.append(("Connection", "keep-alive"))
        fields.append(("Keep-Alive", f"timeout={int(idle_timeout)}, max={remaining}"))

    for name, value in fields:
        out += f"{name}: {value}\r\n".encode("latin-1")
    out += b"\r\n"
    if not head_only:
        out += response.body
    return bytes(out)


# --------------------------------------------------------------------------
# connection loop
# --------------------------------------------------------------------------


class Server:
    def __init__(self, host: str, port: int, *, idle_timeout: float,
                 request_timeout: float, max_requests: int,
                 verbose: bool = True) -> None:
        self.host = host
        self.port = port
        self.idle_timeout = idle_timeout
        self.request_timeout = request_timeout
        self.max_requests = max_requests
        self.verbose = verbose
        self.sock: Optional[socket.socket] = None
        self.connections = 0
        self.requests = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def log(self, *args) -> None:
        if self.verbose:
            print(*args, file=sys.stderr, flush=True)

    def listen(self) -> int:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        return self.port

    def serve_forever(self) -> None:
        assert self.sock is not None
        self.log(f"server11 listening on {self.host}:{self.port} "
                 f"(idle {self.idle_timeout:g}s, max {self.max_requests} req/conn)")
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, peer = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                self.connections += 1
                number = self.connections
            thread = threading.Thread(target=self._guard, args=(conn, peer, number),
                                      daemon=True)
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass

    def _guard(self, conn: socket.socket, peer, number: int) -> None:
        try:
            self.handle_connection(conn, peer, number)
        except Exception as exc:  # never let one connection kill the server
            self.log(f"[conn {number}] internal error: {exc!r}")
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

    def handle_connection(self, conn: socket.socket, peer, number: int) -> None:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        stream = ByteStream(conn)
        served = 0
        self.log(f"[conn {number}] open from {peer[0]}:{peer[1]}")

        while True:
            try:
                stream.wait_for_first_byte(self.idle_timeout)
            except IdleTimeout:
                self.log(f"[conn {number}] idle {self.idle_timeout:g}s, closing "
                         f"after {served} request(s)")
                return
            except ClientGone:
                self.log(f"[conn {number}] peer closed after {served} request(s)")
                return

            conn.settimeout(self.request_timeout)
            head_only = False
            close = False
            request = None

            try:
                method, target, version, headers = parse_head(stream)
                head_only = method == "HEAD"

                framing, length = body_length(method, version, headers)
                if version == "HTTP/1.1":
                    expect = [v.strip().lower()
                              for name, v in headers if name.lower() == "expect"]
                    if expect:
                        if expect != ["100-continue"]:
                            raise HttpError(417, "unsupported Expect")
                        if framing != "none":
                            conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")

                # The body is consumed before dispatch, always. A 405 that
                # leaves the body in the socket desynchronises every request
                # that follows it.
                body = read_body(stream, framing, length)
                request = Request(method, target, version, headers, body)

                if version == "HTTP/1.1":
                    hosts = request.all("host")
                    if len(hosts) != 1:
                        raise HttpError(400, "HTTP/1.1 requires exactly one Host header")

                response = handle(request)

            except HttpError as err:
                response = error_response(err)
                close = err.fatal
            except RequestTimeout:
                self.log(f"[conn {number}] stalled mid-request")
                try:
                    conn.sendall(serialize(error_response(
                        HttpError(408, "request timed out", fatal=True)),
                        head_only=False, close=True,
                        idle_timeout=self.idle_timeout, remaining=0))
                except OSError:
                    pass
                return
            except ClientGone:
                self.log(f"[conn {number}] peer vanished mid-request")
                return

            served += 1
            with self._lock:
                self.requests += 1

            remaining = self.max_requests - served
            if request is not None:
                if request.has_token("connection", "close"):
                    close = True
                elif request.version == "HTTP/1.0" and not request.has_token(
                        "connection", "keep-alive"):
                    close = True
            if remaining <= 0:
                close = True

            payload = serialize(response, head_only=head_only, close=close,
                                idle_timeout=self.idle_timeout,
                                remaining=max(remaining, 0))
            try:
                conn.sendall(payload)
            except OSError:
                self.log(f"[conn {number}] peer gone while writing")
                return

            line = request.method + " " + request.target if request else "<malformed>"
            self.log(f"[conn {number}] #{served} {line} -> {response.status}"
                     + (" (closing)" if close else ""))

            if close:
                return


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="HTTP/1.1 calculator on one socket")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT)
    parser.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT)
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args(argv)

    server = Server(args.host, args.port,
                    idle_timeout=args.idle_timeout,
                    request_timeout=args.request_timeout,
                    max_requests=args.max_requests,
                    verbose=not args.quiet)
    server.listen()

    def shutdown(signum, frame):
        server.stop()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

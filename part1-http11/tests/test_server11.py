#!/usr/bin/env python3
"""Tests for server11.py.

    python3 -m unittest discover -s part1-http11/tests -v

The interesting ones are in FramingTests. Everything else is arithmetic.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server11  # noqa: E402


class ServerFixture(unittest.TestCase):
    idle_timeout = 15.0
    max_requests = 100

    @classmethod
    def setUpClass(cls):
        cls.server = server11.Server(
            "127.0.0.1", 0,
            idle_timeout=cls.idle_timeout,
            request_timeout=5.0,
            max_requests=cls.max_requests,
            verbose=False,
        )
        cls.port = cls.server.listen()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.thread.join(timeout=5)

    # -- helpers ----------------------------------------------------------

    def connect(self, timeout=5.0) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        self.addCleanup(sock.close)
        return sock

    def read_response(self, sock, buf: bytearray):
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed before a complete response")
            buf += chunk
        head, _, rest = bytes(buf).partition(b"\r\n\r\n")
        buf[:] = rest
        lines = head.split(b"\r\n")
        status = int(lines[0].split(b" ")[1])
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            headers[name.decode().lower()] = value.strip().decode()
        length = int(headers.get("content-length", 0))
        while len(buf) < length:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed mid-body")
            buf += chunk
        body = bytes(buf[:length])
        del buf[:length]
        return status, headers, body

    def exchange(self, raw: bytes, count: int = 1):
        """Send raw bytes on a fresh connection, read `count` responses."""
        sock = self.connect()
        sock.sendall(raw)
        buf = bytearray()
        out = [self.read_response(sock, buf) for _ in range(count)]
        return sock, buf, out

    def get(self, target: str, host: str = "localhost", extra: str = ""):
        raw = f"GET {target} HTTP/1.1\r\nHost: {host}\r\n{extra}\r\n".encode()
        _, _, out = self.exchange(raw)
        return out[0]


class ArithmeticTests(ServerFixture):
    def test_the_four_operations(self):
        for target, expected in [("/add?a=2&b=3", b"5"),
                                 ("/sub?a=10&b=4", b"6"),
                                 ("/mul?a=6&b=7", b"42"),
                                 ("/div?a=9&b=3", b"3")]:
            with self.subTest(target=target):
                status, _, body = self.get(target)
                self.assertEqual(status, 200)
                self.assertEqual(body.strip(), expected)

    def test_negative_and_signed_operands(self):
        status, _, body = self.get("/add?a=-5&b=%2B3")
        self.assertEqual(status, 200)
        self.assertEqual(body.strip(), b"-2")

    def test_inexact_division_is_not_silently_truncated(self):
        status, _, body = self.get("/div?a=7&b=2")
        self.assertEqual(status, 200)
        self.assertEqual(body.strip(), b"3.5")

    def test_operand_order_is_not_commutative_for_sub(self):
        self.assertEqual(self.get("/sub?a=4&b=10")[2].strip(), b"-6")


class StatusCodeTests(ServerFixture):
    def test_divide_by_zero_is_400(self):
        self.assertEqual(self.get("/div?a=1&b=0")[0], 400)

    def test_non_integer_operand_is_400(self):
        self.assertEqual(self.get("/add?a=x&b=3")[0], 400)
        self.assertEqual(self.get("/add?a=2.5&b=3")[0], 400)
        self.assertEqual(self.get("/add?a=&b=3")[0], 400)

    def test_missing_operand_is_400(self):
        self.assertEqual(self.get("/add?a=2")[0], 400)

    def test_unknown_path_is_404(self):
        self.assertEqual(self.get("/pow?a=2&b=8")[0], 404)

    def test_wrong_method_is_405_with_allow(self):
        raw = (b"POST /add HTTP/1.1\r\nHost: localhost\r\n"
               b"Content-Length: 0\r\n\r\n")
        _, _, out = self.exchange(raw)
        status, headers, _ = out[0]
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("allow"), "GET, HEAD")

    def test_missing_host_is_400(self):
        raw = b"GET /add?a=2&b=3 HTTP/1.1\r\n\r\n"
        _, _, out = self.exchange(raw)
        self.assertEqual(out[0][0], 400)

    def test_duplicate_host_is_400(self):
        raw = b"GET /add?a=1&b=1 HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n"
        _, _, out = self.exchange(raw)
        self.assertEqual(out[0][0], 400)

    def test_head_sends_headers_without_a_body(self):
        raw = b"HEAD /add?a=2&b=3 HTTP/1.1\r\nHost: localhost\r\n\r\n"
        sock = self.connect()
        sock.sendall(raw)
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            buf += sock.recv(4096)
        head = bytes(buf).split(b"\r\n\r\n")[0]
        self.assertIn(b"200", head.split(b"\r\n")[0])
        self.assertIn(b"Content-Length: 2", head)
        # nothing after the blank line, even though Content-Length says 2
        self.assertEqual(bytes(buf).split(b"\r\n\r\n", 1)[1], b"")

    def test_unknown_query_parameter_is_rejected(self):
        self.assertEqual(self.get("/add?a=1&b=2&c=3")[0], 400)

    def test_error_bodies_are_length_framed_too(self):
        status, headers, body = self.get("/pow?a=1&b=2")
        self.assertEqual(status, 404)
        self.assertEqual(int(headers["content-length"]), len(body))


class FramingTests(ServerFixture):
    """Where does this request end and the next one begin."""

    def test_two_requests_in_one_packet(self):
        raw = (b"GET /add?a=1&b=1 HTTP/1.1\r\nHost: localhost\r\n\r\n"
               b"GET /mul?a=6&b=7 HTTP/1.1\r\nHost: localhost\r\n\r\n")
        _, _, out = self.exchange(raw, count=2)
        self.assertEqual([r[0] for r in out], [200, 200])
        self.assertEqual([r[2].strip() for r in out], [b"2", b"42"])

    def test_body_is_consumed_even_when_the_method_is_rejected(self):
        """The 405 case. If the body is left in the socket, the request
        after it is parsed starting from the body and everything derails."""
        body = b"a=1&b=2"
        raw = (b"POST /add HTTP/1.1\r\nHost: localhost\r\n"
               b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body +
               b"GET /mul?a=3&b=3 HTTP/1.1\r\nHost: localhost\r\n\r\n")
        _, _, out = self.exchange(raw, count=2)
        self.assertEqual(out[0][0], 405)
        self.assertEqual(out[1][0], 200)
        self.assertEqual(out[1][2].strip(), b"9")

    def test_byte_n_plus_one_belongs_to_the_next_request(self):
        """A body whose bytes are themselves a valid request. If the server
        reads one byte too many or too few, the second response is wrong."""
        smuggled = b"GET /mul?a=9&b=9 HTTP/1.1\r\nHost: evil\r\n\r\n"
        raw = (b"POST /add HTTP/1.1\r\nHost: localhost\r\n"
               b"Content-Length: " + str(len(smuggled)).encode() + b"\r\n\r\n" +
               smuggled +
               b"GET /add?a=20&b=22 HTTP/1.1\r\nHost: localhost\r\n\r\n")
        _, _, out = self.exchange(raw, count=2)
        self.assertEqual(out[0][0], 405)
        self.assertEqual(out[1][0], 200)
        # 42, not 81: the smuggled request was body, not request.
        self.assertEqual(out[1][2].strip(), b"42")

    def test_request_split_across_packets(self):
        sock = self.connect()
        for piece in [b"GET /ad", b"d?a=2&b", b"=3 HTTP/1.1\r\nHo",
                      b"st: localhost\r\n", b"\r"]:
            sock.sendall(piece)
            time.sleep(0.01)
        sock.sendall(b"\n")
        buf = bytearray()
        status, _, body = self.read_response(sock, buf)
        self.assertEqual(status, 200)
        self.assertEqual(body.strip(), b"5")

    def test_conflicting_content_lengths_are_rejected(self):
        raw = (b"POST /add HTTP/1.1\r\nHost: localhost\r\n"
               b"Content-Length: 5\r\nContent-Length: 6\r\n\r\nhello")
        _, _, out = self.exchange(raw)
        self.assertEqual(out[0][0], 400)

    def test_content_length_with_transfer_encoding_is_rejected(self):
        raw = (b"POST /add HTTP/1.1\r\nHost: localhost\r\n"
               b"Content-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
        _, _, out = self.exchange(raw)
        self.assertEqual(out[0][0], 400)

    def test_obsolete_line_folding_is_rejected(self):
        raw = b"GET /add?a=1&b=1 HTTP/1.1\r\nHost: localhost\r\n  folded\r\n\r\n"
        _, _, out = self.exchange(raw)
        self.assertEqual(out[0][0], 400)

    def test_garbage_request_line_is_rejected_and_closes(self):
        sock = self.connect()
        sock.sendall(b"NOT-HTTP\r\n\r\n")
        buf = bytearray()
        status, headers, _ = self.read_response(sock, buf)
        self.assertEqual(status, 400)
        self.assertEqual(headers.get("connection"), "close")
        self.assertEqual(sock.recv(1), b"")  # FIN, as promised

    def test_expect_100_continue(self):
        sock = self.connect()
        sock.sendall(b"POST /add HTTP/1.1\r\nHost: localhost\r\n"
                     b"Expect: 100-continue\r\nContent-Length: 4\r\n\r\n")
        interim = sock.recv(64)
        self.assertTrue(interim.startswith(b"HTTP/1.1 100 "), interim)
        sock.sendall(b"abcd")
        buf = bytearray()
        self.assertEqual(self.read_response(sock, buf)[0], 405)


class PersistenceTests(ServerFixture):
    def test_many_requests_on_one_socket(self):
        sock = self.connect()
        buf = bytearray()
        for i in range(25):
            sock.sendall(f"GET /add?a={i}&b={i} HTTP/1.1\r\n"
                         f"Host: localhost\r\n\r\n".encode())
            status, headers, body = self.read_response(sock, buf)
            self.assertEqual(status, 200)
            self.assertEqual(body.strip(), str(i * 2).encode())
            self.assertEqual(headers.get("connection"), "keep-alive")

    def test_connection_close_is_honoured(self):
        sock = self.connect()
        sock.sendall(b"GET /add?a=1&b=1 HTTP/1.1\r\nHost: localhost\r\n"
                     b"Connection: close\r\n\r\n")
        buf = bytearray()
        status, headers, _ = self.read_response(sock, buf)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("connection"), "close")
        self.assertEqual(sock.recv(1), b"")

    def test_http_1_0_closes_unless_it_asks_to_stay(self):
        sock = self.connect()
        sock.sendall(b"GET /add?a=1&b=1 HTTP/1.0\r\n\r\n")
        buf = bytearray()
        status, headers, _ = self.read_response(sock, buf)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("connection"), "close")

    def test_http_1_0_keep_alive_stays(self):
        sock = self.connect()
        buf = bytearray()
        sock.sendall(b"GET /add?a=1&b=1 HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
        status, headers, _ = self.read_response(sock, buf)
        self.assertEqual(headers.get("connection"), "keep-alive")
        sock.sendall(b"GET /mul?a=2&b=2 HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
        self.assertEqual(self.read_response(sock, buf)[2].strip(), b"4")

    def test_keep_alive_header_advertises_the_timeout(self):
        _, headers, _ = self.get("/add?a=1&b=1")
        self.assertIn("timeout=", headers.get("keep-alive", ""))


class LimitTests(ServerFixture):
    max_requests = 3

    def test_request_cap_closes_the_connection(self):
        sock = self.connect()
        buf = bytearray()
        for expected_remaining in (2, 1, 0):
            sock.sendall(b"GET /add?a=1&b=1 HTTP/1.1\r\nHost: localhost\r\n\r\n")
            _, headers, _ = self.read_response(sock, buf)
            if expected_remaining:
                self.assertIn(f"max={expected_remaining}", headers["keep-alive"])
            else:
                self.assertEqual(headers.get("connection"), "close")
        self.assertEqual(sock.recv(1), b"")

    def test_oversized_header_block_is_rejected(self):
        sock = self.connect()
        sock.sendall(b"GET /add?a=1&b=1 HTTP/1.1\r\nHost: localhost\r\n")
        padding = b"X-Pad: " + b"a" * 1000 + b"\r\n"
        buf = bytearray()
        try:
            for _ in range(40):
                sock.sendall(padding)
            sock.sendall(b"\r\n")
            status = self.read_response(sock, buf)[0]
        except (BrokenPipeError, ConnectionResetError):
            return  # also an acceptable answer to a peer that will not stop
        self.assertEqual(status, 431)


class IdleTimeoutTests(ServerFixture):
    idle_timeout = 0.5

    def test_idle_connection_is_closed_after_the_advertised_timeout(self):
        sock = self.connect(timeout=5)
        buf = bytearray()
        sock.sendall(b"GET /add?a=1&b=1 HTTP/1.1\r\nHost: localhost\r\n\r\n")
        _, headers, _ = self.read_response(sock, buf)
        self.assertIn("timeout=0", headers["keep-alive"])
        time.sleep(1.2)
        self.assertEqual(sock.recv(1), b"")

    def test_timeout_never_fires_mid_request(self):
        """A slow sender inside a request must not be hung up on at the
        idle timeout - the idle clock only runs between requests."""
        sock = self.connect(timeout=5)
        sock.sendall(b"GET /add?a=2&b=3 HTTP/1.1\r\n")
        time.sleep(1.0)  # twice the idle timeout, mid-request
        sock.sendall(b"Host: localhost\r\n\r\n")
        buf = bytearray()
        status, _, body = self.read_response(sock, buf)
        self.assertEqual(status, 200)
        self.assertEqual(body.strip(), b"5")


if __name__ == "__main__":
    unittest.main(verbosity=2)

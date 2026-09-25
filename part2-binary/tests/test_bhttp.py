#!/usr/bin/env python3
"""Tests for bserve and bcurl.

    python3 -m unittest discover -s part2-binary/tests -v

Two kinds of test here, and the second kind is the point:

  * bcurl against bserve, which proves the pair works;
  * hand-built frames against bserve, which proves bserve follows SPEC.md
    rather than merely agreeing with whatever bcurl happens to send.

The second kind is what makes this a protocol. If the only client that
ever speaks to the server is the one written beside it, every shared
misreading of the spec stays invisible.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PART2 = os.path.dirname(HERE)
WWW = os.path.join(PART2, "www")
BCURL = os.path.join(PART2, "bcurl")


def load(name: str):
    """Import an extensionless executable as a module."""
    path = os.path.join(PART2, name)
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bserve = load("bserve")

PREFACE = b"BHTTP/1\x00"
T_REQUEST, T_RESPONSE, T_DATA, T_ERROR, T_PING = 1, 2, 3, 4, 5
F_END_MESSAGE, F_ACK = 1, 2


# ---------------------------------------------------------------------------
# a hand-rolled encoder, so the tests do not depend on bcurl being right
# ---------------------------------------------------------------------------


def u24(n):
    return bytes(((n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF))


def u16(n):
    return bytes(((n >> 8) & 0xFF, n & 0xFF))


def frame(ftype, flags, stream, payload):
    return u24(len(payload)) + bytes((ftype, flags)) + u24(stream) + payload


def request_payload(method_code, path, fields=(("host", "localhost"),)):
    out = bytearray([method_code])
    raw = path.encode()
    out += u16(len(raw)) + raw
    out.append(len(fields))
    for name, value in fields:
        index = bserve.NAME_TO_INDEX.get(name, 0)
        out.append(index)
        if index == 0:
            out.append(len(name))
            out += name.encode()
        raw_value = value.encode()
        out += u16(len(raw_value)) + raw_value
    return bytes(out)


class Peer:
    """A raw socket that speaks frames, with none of bcurl's code."""

    def __init__(self, port, send_preface=True):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = bytearray()
        if send_preface:
            self.sock.sendall(PREFACE)

    def send(self, data):
        self.sock.sendall(data)

    def exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def read_frame(self):
        head = self.exact(8)
        length = (head[0] << 16) | (head[1] << 8) | head[2]
        return head[3], head[4], ((head[5] & 0x7F) << 16) | (head[6] << 8) | head[7], \
            self.exact(length)

    def read_message(self):
        """Collect RESPONSE + DATA until END_MESSAGE. Returns (status, body)."""
        status, body = None, bytearray()
        while True:
            ftype, flags, _, payload = self.read_frame()
            if ftype == T_RESPONSE:
                status = (payload[0] << 8) | payload[1]
            elif ftype == T_DATA:
                body += payload
            if flags & F_END_MESSAGE:
                return status, bytes(body)

    def at_eof(self):
        self.sock.setblocking(False)
        try:
            return self.sock.recv(1, socket.MSG_PEEK) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True
        finally:
            try:
                self.sock.setblocking(True)
            except OSError:
                pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class ServerFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = bserve.BServe(WWW, "127.0.0.1", 0, verbose=False)
        cls.port = cls.server.listen()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.thread.join(timeout=5)

    def peer(self, send_preface=True):
        peer = Peer(self.port, send_preface)
        self.addCleanup(peer.close)
        return peer

    def bcurl(self, *args):
        return subprocess.run([sys.executable, BCURL] + list(args),
                              capture_output=True, timeout=30)


class InteropTests(ServerFixture):
    """bcurl against bserve."""

    def test_fetch_a_file(self):
        result = self.bcurl(f"localhost:{self.port}/index.html")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(WWW, "index.html"), "rb") as handle:
            self.assertEqual(result.stdout, handle.read())

    def test_binary_file_survives_byte_for_byte(self):
        result = self.bcurl(f"localhost:{self.port}/assets/blob.bin")
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(WWW, "assets", "blob.bin"), "rb") as handle:
            self.assertEqual(result.stdout, handle.read())

    def test_directory_resolves_to_index(self):
        result = self.bcurl(f"localhost:{self.port}/")
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"BHTTP/1", result.stdout)

    def test_missing_file_is_404_and_a_nonzero_exit(self):
        result = self.bcurl(f"localhost:{self.port}/nope.html")
        self.assertEqual(result.returncode, 4)

    def test_escaping_the_root_is_refused(self):
        result = self.bcurl(f"localhost:{self.port}/../../../etc/passwd")
        self.assertEqual(result.returncode, 4)
        self.assertNotIn(b"root:", result.stdout)

    def test_several_urls_over_one_connection(self):
        before = self.server.connections
        result = self.bcurl("-v", f"localhost:{self.port}/index.html",
                            "/about.html", "/assets/data.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.server.connections - before, 1,
                         "bcurl opened more than one connection")
        self.assertIn(b"1 TCP handshake, 3 request(s)", result.stderr)

    def test_verbose_hexdumps_both_directions(self):
        result = self.bcurl("-v", f"localhost:{self.port}/index.html")
        self.assertIn(b"> REQUEST", result.stderr)
        self.assertIn(b"< RESPONSE", result.stderr)
        self.assertIn(b"connection preface", result.stderr)

    def test_head_sends_no_body(self):
        result = self.bcurl("-I", f"localhost:{self.port}/index.html")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"content-length: 350", result.stderr)

    def test_unknown_frame_from_the_client_is_skipped_by_the_server(self):
        result = self.bcurl("--send-unknown", f"localhost:{self.port}/about.html")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"<h1>about</h1>", result.stdout)

    def test_ping_round_trip(self):
        result = self.bcurl("-v", "--ping", f"localhost:{self.port}/index.html")
        self.assertIn(b"PING acknowledged", result.stderr)


class WireTests(ServerFixture):
    """Hand-built frames, checked against SPEC.md rather than against bcurl."""

    def test_minimal_request(self):
        peer = self.peer()
        peer.send(frame(T_REQUEST, F_END_MESSAGE, 1,
                        request_payload(1, "/index.html")))
        status, body = peer.read_message()
        self.assertEqual(status, 200)
        self.assertIn(b"BHTTP/1", body)

    def test_unknown_frame_type_is_skipped_and_the_connection_lives(self):
        """SPEC 10, the line that may not be skipped.

        If the server does not consume exactly Length octets of a frame it
        does not understand, the request that follows starts in the middle
        of this payload and the answer is nonsense or nothing at all.
        """
        peer = self.peer()
        noise = b"\x01\x00\x0b/index.html" * 4  # looks like a REQUEST payload
        peer.send(frame(0x7F, 0x00, 0, noise))
        peer.send(frame(T_REQUEST, F_END_MESSAGE, 1,
                        request_payload(1, "/about.html")))
        status, body = peer.read_message()
        self.assertEqual(status, 200)
        self.assertIn(b"<h1>about</h1>", body)

    def test_experimental_frame_range_is_also_skipped(self):
        peer = self.peer()
        peer.send(frame(0xF0, 0xFF, 0, b"private use"))
        peer.send(frame(T_REQUEST, F_END_MESSAGE, 1, request_payload(1, "/")))
        self.assertEqual(peer.read_message()[0], 200)

    def test_reserved_bit_is_ignored_not_validated(self):
        """SPEC 3: receivers MUST ignore R. Set it and expect a normal answer."""
        payload = request_payload(1, "/index.html")
        raw = bytearray(frame(T_REQUEST, F_END_MESSAGE, 1, payload))
        raw[5] |= 0x80  # the reserved bit
        peer = self.peer()
        peer.send(bytes(raw))
        self.assertEqual(peer.read_message()[0], 200)

    def test_reserved_flag_bits_are_ignored(self):
        peer = self.peer()
        peer.send(frame(T_REQUEST, F_END_MESSAGE | 0xF0, 1,
                        request_payload(1, "/index.html")))
        self.assertEqual(peer.read_message()[0], 200)

    def test_truncated_header_block_is_400_and_the_connection_lives(self):
        """A parse failure inside a frame is recoverable precisely because
        the frame header already said where the frame ended."""
        peer = self.peer()
        broken = request_payload(1, "/index.html")[:-3]
        peer.send(frame(T_REQUEST, F_END_MESSAGE, 1, broken))
        self.assertEqual(peer.read_message()[0], 400)

        peer.send(frame(T_REQUEST, F_END_MESSAGE, 3, request_payload(1, "/")))
        self.assertEqual(peer.read_message()[0], 200)
        self.assertFalse(peer.at_eof())

    def test_unknown_header_index_is_400_not_a_guess(self):
        payload = bytearray([1])
        payload += u16(11) + b"/index.html"
        payload += bytes([1, 99]) + u16(2) + b"hi"   # index 99 is unassigned
        peer = self.peer()
        peer.send(frame(T_REQUEST, F_END_MESSAGE, 1, bytes(payload)))
        self.assertEqual(peer.read_message()[0], 400)

    def test_uppercase_literal_header_name_is_400(self):
        peer = self.peer()
        peer.send(frame(T_REQUEST, F_END_MESSAGE, 1,
                        request_payload(1, "/", (("X-Loud", "yes"),))))
        self.assertEqual(peer.read_message()[0], 400)

    def test_non_origin_form_path_is_400(self):
        peer = self.peer()
        peer.send(frame(T_REQUEST, F_END_MESSAGE, 1,
                        request_payload(1, "http://elsewhere/index.html")))
        self.assertEqual(peer.read_message()[0], 400)

    def test_unknown_method_code_is_501(self):
        peer = self.peer()
        peer.send(frame(T_REQUEST, F_END_MESSAGE, 1,
                        request_payload(200, "/index.html")))
        self.assertEqual(peer.read_message()[0], 501)

    def test_post_is_405_with_allow(self):
        peer = self.peer()
        peer.send(frame(T_REQUEST, F_END_MESSAGE, 1,
                        request_payload(3, "/index.html")))
        ftype, flags, _, payload = peer.read_frame()
        self.assertEqual(ftype, T_RESPONSE)
        self.assertEqual((payload[0] << 8) | payload[1], 405)
        self.assertIn(b"allow", payload)

    def test_two_requests_in_one_write(self):
        """The binary equivalent of pipelining. One send(), two answers,
        in order, because every frame carries its own length."""
        peer = self.peer()
        peer.send(frame(T_REQUEST, F_END_MESSAGE, 1, request_payload(1, "/index.html"))
                  + frame(T_REQUEST, F_END_MESSAGE, 3, request_payload(1, "/about.html")))
        first = peer.read_message()
        second = peer.read_message()
        self.assertIn(b"BHTTP/1", first[1])
        self.assertIn(b"<h1>about</h1>", second[1])

    def test_frame_split_across_packets(self):
        peer = self.peer()
        raw = frame(T_REQUEST, F_END_MESSAGE, 1, request_payload(1, "/index.html"))
        for index in range(0, len(raw), 3):
            peer.send(raw[index : index + 3])
            time.sleep(0.005)
        self.assertEqual(peer.read_message()[0], 200)

    def test_even_stream_id_is_a_connection_error(self):
        peer = self.peer()
        peer.send(frame(T_REQUEST, F_END_MESSAGE, 2, request_payload(1, "/")))
        ftype, _, _, payload = peer.read_frame()
        self.assertEqual(ftype, T_ERROR)
        self.assertEqual((payload[0] << 8) | payload[1], 1)  # PROTOCOL_ERROR

    def test_oversized_frame_is_refused_and_closed(self):
        """We cannot skip a frame we will not read, so the stream position
        is lost and the connection has to go (SPEC 11)."""
        peer = self.peer()
        peer.send(u24(0xFFFFFF) + bytes((T_REQUEST, F_END_MESSAGE)) + u24(1))
        ftype, _, _, payload = peer.read_frame()
        self.assertEqual(ftype, T_ERROR)
        self.assertEqual((payload[0] << 8) | payload[1], 2)  # FRAME_TOO_LARGE

    def test_bad_preface_is_refused(self):
        peer = self.peer(send_preface=False)
        peer.send(b"GET / HTTP/1.1\r\n")
        ftype, _, _, payload = peer.read_frame()
        self.assertEqual(ftype, T_ERROR)
        self.assertEqual((payload[0] << 8) | payload[1], 3)  # BAD_PREFACE

    def test_ping_is_echoed_with_ack(self):
        peer = self.peer()
        peer.send(frame(T_PING, 0, 0, b"marco"))
        ftype, flags, stream, payload = peer.read_frame()
        self.assertEqual((ftype, stream, payload), (T_PING, 0, b"marco"))
        self.assertTrue(flags & F_ACK)

    def test_a_client_may_not_send_response(self):
        peer = self.peer()
        peer.send(frame(T_RESPONSE, F_END_MESSAGE, 1, u16(200) + bytes([0])))
        self.assertEqual(peer.read_frame()[0], T_ERROR)

    def test_many_requests_on_one_connection(self):
        peer = self.peer()
        for index in range(20):
            stream = 1 + index * 2
            peer.send(frame(T_REQUEST, F_END_MESSAGE, stream,
                            request_payload(1, "/assets/data.json")))
            status, body = peer.read_message()
            self.assertEqual(status, 200)
            self.assertIn(b"BHTTP/1", body)
        self.assertFalse(peer.at_eof())


if __name__ == "__main__":
    unittest.main(verbosity=2)

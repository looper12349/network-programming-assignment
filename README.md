# stay-on-the-line

Two exercises in speaking a protocol over one socket, for the Network
Architecture course.

- **`part1-http11/`** — an HTTP/1.1 calculator that keeps the connection open.
- **`part2-binary/`** — BHTTP/1: a binary framing of HTTP, with a server, a
  client, and [the spec](SPEC.md) between them.

Python 3.9+, standard library only. No frameworks, no `http.server`, no
`requests` — just `socket`.

Both halves answer the same question, which HTTP/1.0 never had to ask:

> **where does this request end and the next one begin?**

Part 1 answers it with `Content-Length`, and gets it wrong in interesting ways
if you are careless. Part 2 answers it with a length in a fixed-size header,
which is what every binary protocol since has done, for the reasons set out in
[`SPEC.md` §3.1](SPEC.md).

---

## Part 1 — a calculator that stays on the line

```
$ python3 part1-http11/server11.py --port 8080
```

| Request | Response |
|---|---|
| `GET /add?a=2&b=3` | `200` `5` |
| `GET /sub?a=10&b=4` | `200` `6` |
| `GET /mul?a=6&b=7` | `200` `42` |
| `GET /div?a=9&b=3` | `200` `3` |
| `GET /div?a=1&b=0` | `400` |
| `GET /add?a=x&b=3` | `400` |
| `GET /pow?a=2&b=8` | `404` |
| `POST /add` | `405` + `Allow: GET, HEAD` |
| `GET /add` with no `Host` | `400` |

### The marking harness

`check.py` writes out what the marker does, so I fail it before he does. It
runs the whole set twice on one socket: once request-by-request, then with all
of them in a single `send()`, which is pipelining.

```
$ python3 part1-http11/check.py --spawn

--- sequential ---
  ok    GET  /add?a=2&b=3     -> 200 5
  ok    GET  /sub?a=10&b=4    -> 200 6
  ok    GET  /mul?a=6&b=7     -> 200 42
  ok    GET  /div?a=9&b=3     -> 200 3
  ok    GET  /div?a=1&b=0     -> 400 400 Bad Request: division by zero
  ok    GET  /add?a=x&b=3     -> 400 400 Bad Request: a is not an integer: 'x'
  ok    GET  /pow?a=2&b=8     -> 404 404 Not Found: no such operation: /pow
  ok    POST /add             -> 405 405 Method Not Allowed: POST is not allowed here

  socket still open: True
  1 TCP handshake, 8 responses (0.002s)
  reuse after the marking set: 200 42

--- pipelined (one send, six answers in order) ---
  [the same eight, in the same order]

  socket still open: True
  1 TCP handshake, 8 responses (0.001s)

PASS
```

### What is actually hard, and how it is handled

**The connection owns a buffer, not the parser.** `recv()` returns whatever the
kernel happens to hold — half a request line, or two whole requests and the
first byte of a third. Both are normal. `ByteStream` hands out exactly what was
asked for and keeps the remainder, so nothing above it ever has to guess.

**The body is consumed before the request is dispatched**, even when the answer
is going to be `405`. A rejected `POST` whose body is left in the socket
desynchronises every request after it. `test_byte_n_plus_one_belongs_to_the_next_request`
sends a body that is itself a valid `GET /mul?a=9&b=9` and checks that the next
response is `42`, not `81`.

**Errors come in two kinds.** If the framing is still known — a bad operand, a
missing `Host`, an unknown path — the server answers and stays on the line. If
the framing itself is in doubt — a malformed request line, two disagreeing
`Content-Length` headers, `Transfer-Encoding` and `Content-Length` together —
it answers once, says `Connection: close`, and hangs up. Guessing where the
next request starts is how you get request smuggling.

**The idle timeout only runs between requests.** A timeout that can fire while
the peer is mid-sentence is not one you can defend. A stalled request gets
`408` on a separate, longer clock.

**Stretch goals, all done:** `Connection: close` honoured; an idle timeout,
advertised in `Keep-Alive: timeout=…, max=…`; chunked request bodies decoded,
trailers included; and pipelining, which a buffered reader that answers in
order gets almost for free.

### Tests

```
$ python3 -m unittest discover -s part1-http11/tests -t part1-http11/tests
34 tests
```

One of them found a real bug. The header-size limit was only checked when the
delimiter had not arrived yet, so a peer that sent 40 KiB of headers in a
single write sailed straight past it — one `recv()` can deliver 64 KiB. A
limit that only fires when the peer is slow is not a limit.

---

## Part 2 — HTTP, in binary

The spec is [`SPEC.md`](SPEC.md). The two programs were written from it and
**share no code**: `bserve` has its own decoder, `bcurl` its own encoder. A
client that only works against its own server's encoder is an implementation,
not a protocol — so `tests/test_bhttp.py` adds a third implementation, a
hand-rolled frame builder that checks `bserve` against the document rather
than against `bcurl`.

### The frame header

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+---------------------------------------------------------------+
|                    Length (24)                |    Type (8)   |
+---------------------------------------------------------------+
|    Flags (8)  |R|              Stream ID (23)                 |
+---------------------------------------------------------------+
```

Eight octets. HTTP/2 spends nine — 24 / 8 / 8 / 1+31 — and the argument for
each of those numbers, plus the argument for changing the last, is in
[`SPEC.md` §3.1](SPEC.md). Short version: the stream id drops from 31 bits to
23 so the header is a power of two and every payload lands 8-octet aligned, at
the stated cost of a 4.19-million-stream ceiling per connection.

Header fields use HPACK's first two mechanisms and nothing else: **ten numbered
names**, and **a length prefix on everything else**. No dynamic table, so a
header block decodes in isolation.

### Running it

```
$ ./part2-binary/bserve ./part2-binary/www 9000
$ ./part2-binary/bcurl -v localhost:9000/index.html
```

Several URLs go over one connection, and `bcurl` never opens a second:

```
$ ./part2-binary/bcurl -v localhost:9000/index.html /about.html /assets/data.json
...
* 1 TCP handshake, 3 request(s), 0 unknown frame(s) skipped
```

Other flags: `-I` (HEAD), `-o FILE`, `-X METHOD`, `--ping`, and
`--send-unknown`, which deliberately sends a frame type this version does not
define so you can watch the server step over it.

Exit codes: `0` ok, `2` usage, `3` connection or protocol failure, `4` on 4xx,
`5` on 5xx.

### The line that may not be skipped

> A receiver that meets a frame whose `Type` it does not implement **MUST**
> read and discard exactly `Length` octets, and then continue with the next
> frame. It **MUST NOT** close the connection and **MUST NOT** send an error.

That sentence is what makes a version 2 possible, and it works only because
`Length` sits in a fixed position in a fixed-size header, where a receiver can
find it without understanding anything else in the frame.

```
$ ./part2-binary/bcurl --send-unknown localhost:9000/about.html
[bserve] skipped unknown frame type 0x7f (23 octets)
[bserve] #1 stream=1 GET /about.html -> 200 (257 octets)
```

`test_unknown_frame_type_is_skipped_and_the_connection_lives` makes the payload
of that unknown frame *look like* a valid `REQUEST` payload, so a server that
skipped it by guessing rather than by length would answer the wrong thing and
be caught.

### The annotated hexdump

[`docs/annotated-hexdump.md`](docs/annotated-hexdump.md) — one complete request
and response, captured from the wire, every octet accounted for against the
section of the spec that defines it.

### Tests

```
$ python3 -m unittest discover -s part2-binary/tests -t part2-binary/tests
29 tests
```

Ten are `bcurl` against `bserve`. Nineteen are hand-built frames against
`bserve`: reserved bits set, unknown header indices, truncated header blocks,
uppercase literal names, even stream ids, an oversized `Length`, a bad preface,
two requests in one write, and a frame dribbled out three octets at a time.

---

## Layout

```
SPEC.md                          the contract — enough for a stranger
part1-http11/
  server11.py                    HTTP/1.1 calculator, persistent connections
  check.py                       the marking harness
  tests/test_server11.py         34 tests, including the byte n+1 trap
part2-binary/
  bserve                         BHTTP/1 file server      (own decoder)
  bcurl                          BHTTP/1 client           (own encoder)
  www/                           document root
  tests/test_bhttp.py            29 tests, incl. a third frame implementation
docs/annotated-hexdump.md        one exchange, every octet explained
```

## Everything at once

```
$ make test      # both suites
$ make check     # the marking harness
$ make demo      # part 2 end to end, with hexdumps
```

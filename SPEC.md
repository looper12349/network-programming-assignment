# BHTTP/1 — HTTP, in binary

**Version 1.0 · 2026-09-23 · Amritesh Indal**

BHTTP/1 carries HTTP request and response semantics over one TCP connection as
a stream of self-describing binary frames. It keeps HTTP's methods, paths,
header fields and status codes, and replaces only the syntax: no CRLF, no
case-insensitive text, no scanning for a delimiter. Every structure in this
protocol is preceded by its own length.

This document is the contract. A client written only from this page and a
server written only from this page must interoperate; neither implementation is
normative.

---

## 1. Conventions

**MUST**, **MUST NOT**, **SHOULD** and **MAY** are used as in RFC 2119.

All multi-octet integers are unsigned and **big-endian** (network byte order).
"Octet" means an 8-bit byte. All offsets are zero-based. Text fields are UTF-8
unless stated otherwise; header field names are lowercase US-ASCII.

A *peer* is either endpoint. A *receiver* is the peer reading a given frame.

---

## 2. Connection

**2.1** BHTTP/1 runs over a single TCP connection. The connection carries many
messages in both directions and **MUST** stay open until a peer deliberately
closes it. There is no "one request per connection" mode.

**2.2 Preface.** Immediately after the TCP handshake, and before any frame, the
client **MUST** send exactly eight octets:

```
42 48 54 54 50 2f 31 00        "BHTTP/1\0"
```

The server **MUST** read all eight before parsing anything else, and **MUST
NOT** send any octet before it has. If the octets differ, the server **MUST**
send an `ERROR` frame with code `BAD_PREFACE` and close the connection.

Why a preface at all: the first thing a receiver needs is not a frame, it is
the answer to *"is the thing on the other end speaking my protocol, and which
version of it?"*. Guessing that from the first frame header means a peer
speaking something else entirely gets a 400 rather than an honest "wrong
protocol", and it forecloses BHTTP/2 having a different frame header. Eight
octets also puts the first frame header at offset 8, so every frame header in
the connection begins on an 8-octet boundary.

---

## 3. Frame layout

Every frame is an 8-octet header followed by exactly `Length` octets of
payload. There is nothing else on the wire.

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+---------------------------------------------------------------+
|                    Length (24)                |    Type (8)   |
+---------------------------------------------------------------+
|    Flags (8)  |R|              Stream ID (23)                 |
+---------------------------------------------------------------+
|                    Payload (Length * 8)                     ...
+---------------------------------------------------------------+
```

| Offset | Width | Field | Meaning |
|---|---|---|---|
| 0–2 | 24 bits | `Length` | Payload octets following this header. Excludes the header itself. |
| 3 | 8 bits | `Type` | Frame type (§4). |
| 4 | 8 bits | `Flags` | Type-specific bit field (§5). |
| 5 (MSB) | 1 bit | `R` | Reserved. Senders **MUST** set it to 0. Receivers **MUST** ignore it. |
| 5–7 | 23 bits | `Stream ID` | Message this frame belongs to (§8). |

Total: **8 octets**.

### 3.1 Why these widths

HTTP/2 chose 24 / 8 / 8 / 1+31 and a 9-octet header. The reasoning below is why
this protocol keeps three of those numbers and changes the fourth.

**`Length` — 24 bits.** At 16 bits the ceiling is 64 KiB, so every real file
gets fragmented and pays a header per chunk for no benefit. At 32 bits a peer
can announce 4 GiB, and the receiver must then either trust it — which is an
unauthenticated allocation, i.e. a way to be killed by a stranger — or impose a
second, smaller limit anyway, which means the extra 8 bits were never usable.
24 bits announces a maximum (16 MiB) that a receiver can plausibly buffer,
and this protocol still caps the *accepted* payload at 1 MiB by policy (§11):
the field says what is expressible, the policy says what is acceptable, and
they are not the same question.

**`Type` — 8 bits.** One octet, 256 types, no bit-shifting to read it. Four
bits would save nothing once the header is aligned and would leave 16 types to
cover all future extension, which is how you end up overloading flags to mean
new frame types.

**`Flags` — 8 bits.** A per-type bit field. Eight is what fills the octet;
this version defines two bits and reserves six.

**`Stream ID` — 23 bits, and this is the deliberate departure.** HTTP/2 spends
31 bits plus a reserved bit, which makes the header 9 octets — an odd number,
so payloads land on odd boundaries forever after. Dropping to 23 bits puts the
header at 8 octets, keeps every header *and* every payload 8-octet aligned, and
still allows 2^22 ≈ 4.19 million client-initiated streams before the space is
exhausted. The trade-off is explicit: a connection that serves four million
requests must be replaced, where HTTP/2 tolerates a billion. That is a cost
paid roughly never, for an alignment benefit paid on every frame.

The reserved bit survives the shrink on purpose. HTTP/2's reserved bit is
famously the one field implementations got wrong by *validating* it, which is
why it can never be used. Here the rule is stated as a requirement on the
receiver, not the sender: **a receiver MUST ignore `R`**. A bit nobody ignores
is a bit nobody can ever spend.

---

## 4. Frame types

| Value | Name | Direction | Purpose |
|---|---|---|---|
| `0x00` | — | — | Reserved. **MUST NOT** be sent. Treated as unknown on receipt (§10). |
| `0x01` | `REQUEST` | client → server | Method, path and header fields. Starts a stream. |
| `0x02` | `RESPONSE` | server → client | Status code and header fields. |
| `0x03` | `DATA` | either | Message body octets. |
| `0x04` | `ERROR` | either | Connection-level failure. Sent immediately before closing. |
| `0x05` | `PING` | either | Liveness check. The receiver echoes the payload with `ACK` set. |
| `0x06`–`0x7f` | — | — | Unassigned. Reserved for future versions of this spec. |
| `0x80`–`0xff` | — | — | Available for private and experimental use. |

A peer **MUST NOT** send a frame type in a direction the table forbids. A
server receiving `RESPONSE`, or a client receiving `REQUEST`, **MUST** treat it
as a connection error (§9.2).

## 5. Flags

| Bit | Value | Name | Applies to |
|---|---|---|---|
| 0 | `0x01` | `END_MESSAGE` | `REQUEST`, `RESPONSE`, `DATA` |
| 1 | `0x02` | `ACK` | `PING` |
| 2–7 | `0xfc` | — | Reserved. Senders **MUST** set to 0; receivers **MUST** ignore. |

`END_MESSAGE` marks the last frame of a message on its stream. A frame carrying
a flag that is undefined *for its type* is not an error: the receiver ignores
that bit and processes the frame normally.

## 6. Header blocks

`REQUEST` and `RESPONSE` payloads each contain one header block. This is
HPACK's first two mechanisms and nothing more: **a static table of the names we
actually send**, and **a length prefix on everything else**. There is no
dynamic table, no Huffman coding, and therefore no shared state between frames
— a header block can be decoded in isolation.

```
count        u8         number of entries, 0..64
entry        (repeated `count` times):
  name_ref   u8         1..10 -> static table; 0 -> literal name follows
  name_len   u8         present only when name_ref == 0
  name       name_len octets, lowercase US-ASCII token
  value_len  u16
  value      value_len octets, UTF-8
```

### 6.1 Static name table

| Index | Name | | Index | Name |
|---|---|---|---|---|
| 1 | `host` | | 6 | `date` |
| 2 | `user-agent` | | 7 | `server` |
| 3 | `accept` | | 8 | `connection` |
| 4 | `content-type` | | 9 | `last-modified` |
| 5 | `content-length` | | 10 | `etag` |

These are the ten names this protocol actually sends, which is the only reason
a static table is worth having. Indices `11`–`255` are unassigned; a receiver
meeting one **MUST** answer `400` (§9.1). Unlike an unknown *frame type*, an
unknown header index cannot be skipped intelligibly — the octets are present
and parseable, but their meaning is not, and guessing is worse than refusing.

Names sent as literals **MUST** be lowercase. A receiver meeting an uppercase
octet in a literal name **MUST** answer `400`; case-insensitive comparison is a
class of bug this protocol declines to inherit.

## 7. Payloads

### 7.1 `REQUEST`

```
method       u8      1 GET, 2 HEAD, 3 POST, 4 PUT, 5 DELETE, 6 OPTIONS
                     0 -> literal method follows
method_len   u8      present only when method == 0
method_name  method_len octets, uppercase US-ASCII token
path_len     u16     1..8192
path         path_len octets, origin-form, MUST begin with '/'
header block (§6)
```

The path is origin-form only: no scheme, no authority. The authority belongs in
a `host` header field, exactly as in HTTP/1.1, and a client **SHOULD** send one.

### 7.2 `RESPONSE`

```
status       u16     an HTTP status code, 100..599
header block (§6)
```

### 7.3 `DATA`

The payload is `Length` octets of body, opaque to the framing layer.

### 7.4 `ERROR`

```
code         u16     §9.2
reason_len   u16
reason       reason_len octets, UTF-8, for humans only
```

A receiver **MUST NOT** make decisions based on `reason`.

### 7.5 `PING`

0 to 64 octets, echoed back verbatim with `ACK` set. A peer **MUST NOT** reply
to a `PING` that already has `ACK` set.

## 8. Messages and streams

**8.1** A *message* is one `REQUEST` or `RESPONSE` frame, followed by zero or
more `DATA` frames, on one stream. The final frame of the message carries
`END_MESSAGE`. A message with no body sets `END_MESSAGE` on its `REQUEST` or
`RESPONSE` frame directly.

**8.2** Streams are identified by the `Stream ID` field. The client **MUST**
use odd identifiers, starting at 1 and increasing by 2 for each request. A
server **MUST** respond on the stream the request arrived on, and in this
version **MUST NOT** originate a stream.

**8.3** Stream ID `0` is the connection itself. `ERROR` and `PING` **MUST** use
it; `REQUEST`, `RESPONSE` and `DATA` **MUST NOT**.

**8.4** A client **MUST NOT** reuse a stream identifier, and **MUST NOT** send
an identifier lower than one it has already used. A server meeting either
**MUST** treat it as a connection error. When the space is exhausted, a client
opens a new connection.

**8.5** In this version a server answers requests **in the order it received
them**. Interleaving responses is what the stream identifier exists to permit,
and it is left to BHTTP/2; a v1 receiver may rely on ordering, and a v1 sender
must not assume the peer can cope without it.

**8.6** If a `content-length` header field is present it **MUST** equal the sum
of the `DATA` payload lengths for that message. It is advisory: the framing is
authoritative, and a receiver that finds them in conflict **MUST** trust the
frames and **MUST NOT** use the header to decide where the body ends. This
protocol has exactly one answer to "where does this end", which is the
difference between it and the one it replaces.

## 9. Errors

### 9.1 Stream errors — answered, connection survives

Sent as a `RESPONSE` frame on the request's stream.

| Status | When |
|---|---|
| `400` | The frame's payload does not parse: short header block, bad length prefix, unknown header index, non-origin-form path, uppercase literal name. |
| `404` | The path does not name a resource. |
| `405` | The method is known but not allowed for this resource. `allow` **SHOULD** be sent. |
| `501` | The method is not implemented. |
| `500` | The receiver failed for its own reasons. |

Every one of these leaves the connection open. This is possible *because* the
frame header told the receiver where the frame ended before it tried to parse
the contents: the parse can fail without the stream position being in doubt.
That property is the entire argument for length-prefixed framing, and an
implementation that closes the connection on a `400` has thrown it away.

### 9.2 Connection errors — `ERROR` frame, then close

| Code | Name | When |
|---|---|---|
| `1` | `PROTOCOL_ERROR` | A rule in §4 or §8 was broken; the stream position is no longer trustworthy. |
| `2` | `FRAME_TOO_LARGE` | `Length` exceeds the receiver's limit (§11). |
| `3` | `BAD_PREFACE` | §2.2. |
| `4` | `INTERNAL_ERROR` | The peer is shutting down. |

After sending `ERROR` a peer **MUST** close the connection, and **MUST NOT**
send further frames.

## 10. Extensibility — the rule you may not skip

> **A receiver that meets a frame whose `Type` it does not implement MUST read
> and discard exactly `Length` octets, and then continue with the next frame.
> It MUST NOT close the connection, MUST NOT send an error, and MUST NOT
> attempt to interpret the payload.**

This single sentence is what makes a version 2 possible. A new frame type can
be deployed on a live connection, and a peer that predates it will step over
the frame cleanly rather than desynchronising or hanging up. It works only
because `Length` is in a fixed position in a fixed-size header, which a
receiver can parse without understanding anything else in the frame — the
reason the length lives in the header and not in the payload.

The same applies to reserved flag bits (§5) and the reserved header bit (§3):
ignore what you do not know, at every level where "ignore" still leaves the
stream position certain. Where it does not — an unknown header index inside an
otherwise valid block (§6.1) — the answer is a stream error, never a guess.

## 11. Limits

| Limit | Value | Reason |
|---|---|---|
| Maximum accepted payload | 1 MiB | A receiver allocates on a stranger's say-so; the bound is the receiver's, not the sender's. |
| Maximum header block entries | 64 | `count` is `u8`; 64 is generous for real traffic. |
| Maximum path length | 8192 octets | Matches typical HTTP/1.1 server limits. |
| Maximum `PING` payload | 64 octets | It is a liveness check, not a channel. |

A receiver meeting `Length` greater than its limit **MUST** send
`ERROR(FRAME_TOO_LARGE)` and close: it cannot skip a frame it refuses to read,
so the stream position is lost and the connection is no longer usable.

## 12. Worked example

`GET /index.html` with a `host` and a `user-agent` field, on stream 1:

```
00 00 39 01 01 00 00 01   header: len=57 type=REQUEST flags=END_MESSAGE stream=1
01                        method = 1 (GET)
00 0b                     path_len = 11
2f 69 6e 64 65 78 2e 68   "/index.h"
74 6d 6c                  "tml"
02                        2 header entries
01                          [1] host
00 0e                       value_len = 14
   6c 6f 63 61 6c 68 6f 73 74 3a 39 30 30 30     "localhost:9000"
02                          [2] user-agent
00 08                       value_len = 8
   62 63 75 72 6c 2f 31 2e 30                    "bcurl/1.0"
```

A complete annotated exchange, taken from the wire rather than written by hand,
is in `docs/annotated-hexdump.md`.

## 13. Conformance checklist

A conforming implementation:

1. sends the preface before any frame, and reads it before parsing any (§2.2);
2. never reads past `Length` octets of a frame, and never stops short (§3);
3. skips unknown frame types by `Length` and keeps the connection (§10);
4. ignores the reserved bit and reserved flag bits rather than validating them;
5. answers `400`/`404`/`405` as stream errors without closing (§9.1);
6. closes only on the four connection errors in §9.2;
7. trusts frame boundaries over `content-length` when they disagree (§8.6);
8. serves many messages over one connection and opens no second connection
   while the first is usable.

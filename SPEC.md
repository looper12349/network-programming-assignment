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

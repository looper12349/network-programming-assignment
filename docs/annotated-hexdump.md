# One complete exchange, annotated

Captured from the wire, not written by hand:

```
$ ./bserve ./www 9100 &
$ ./bcurl -v localhost:9100/hello.txt
hello, framed world
```

`www/hello.txt` is 20 octets: `hello, framed world\n`.

Every octet the two programs exchanged is below, in order, with what each one
means and which section of [`SPEC.md`](../SPEC.md) says so. Nothing is elided.

---

## 1. Connection preface — 8 octets, client to server (SPEC 2.2)

```
42 48 54 54 50 2f 31 00                            |BHTTP/1.|
```

| Octets | Value | Meaning |
|---|---|---|
| 0–7 | `42 48 54 54 50 2f 31 00` | `"BHTTP/1\0"` |

Sent once, before any frame. The server reads all eight before parsing
anything, and sends nothing until it has. Because the preface is eight octets,
the first frame header begins at connection offset 8 — and since every frame
header is also eight octets, every subsequent one is 8-octet aligned too.

---

## 2. REQUEST frame — 8 + 49 octets, client to server (SPEC 3, 7.1)

```
0000  00 00 31 01 01 00 00 01  01 00 0a 2f 68 65 6c 6c  |..1......../hell|
0010  6f 2e 74 78 74 03 01 00  0e 6c 6f 63 61 6c 68 6f  |o.txt....localho|
0020  73 74 3a 39 31 30 30 02  00 09 62 63 75 72 6c 2f  |st:9100...bcurl/|
0030  31 2e 30 03 00 03 2a 2f  2a                       |1.0...*/*|
```

### 2.1 Frame header (offsets 0x00–0x07)

| Offset | Octets | Field | Value |
|---|---|---|---|
| 0x00 | `00 00 31` | `Length` (24) | 49 — payload octets that follow this header |
| 0x03 | `01` | `Type` (8) | `REQUEST` |
| 0x04 | `01` | `Flags` (8) | `END_MESSAGE`: no `DATA` frames follow, the message is this one frame |
| 0x05 | `00 00 01` | `R` (1) + `Stream ID` (23) | top bit 0 (reserved, sent 0, ignored on receipt); stream 1 — odd, as every client-initiated stream must be |

Eight octets, and the receiver now knows exactly where this frame ends —
offset 0x08 + 49 = 0x39 — **before it has understood a single octet of the
payload**. That is the whole reason the rest of this protocol can recover from
a bad frame without hanging up.

### 2.2 Request payload (offsets 0x08–0x38)

| Offset | Octets | Meaning |
|---|---|---|
| 0x08 | `01` | `method` = 1 = `GET` |
| 0x09 | `00 0a` | `path_len` = 10 |
| 0x0b | `2f 68 65 6c 6c 6f 2e 74 78 74` | `/hello.txt` |
| 0x15 | `03` | header block: 3 entries |

Header block (SPEC 6). Each entry is one octet of name reference, then a
16-bit length, then the value. No Huffman coding, no dynamic table, so this
block means the same thing read in isolation as it does read in sequence.

| Offset | Octets | Meaning |
|---|---|---|
| 0x16 | `01` | name index 1 → `host` |
| 0x17 | `00 0e` | value length 14 |
| 0x19 | `6c 6f 63 61 6c 68 6f 73 74 3a 39 31 30 30` | `localhost:9100` |
| 0x27 | `02` | name index 2 → `user-agent` |
| 0x28 | `00 09` | value length 9 |
| 0x2a | `62 63 75 72 6c 2f 31 2e 30` | `bcurl/1.0` |
| 0x33 | `03` | name index 3 → `accept` |
| 0x34 | `00 03` | value length 3 |
| 0x36 | `2a 2f 2a` | `*/*` |

Three header names cost three octets instead of the 32 the same names cost in
HTTP/1.1 (`Host: `, `User-Agent: `, `Accept: ` plus their CRLFs). That is the
entire benefit of the static table, and the reason it holds ten names rather
than sixty-one: these are the names this protocol actually sends.

The payload ends exactly at 0x38, which is `Length` − 1 from its start. If it
had not — if the last value ran one octet past the end, or stopped one short —
the server would answer `400` and keep the connection, because the frame
boundary was never in question.

---

## 3. RESPONSE frame — 8 + 137 octets, server to client (SPEC 7.2)

```
0000  00 00 89 02 00 00 00 01  00 c8 06 04 00 19 74 65  |..............te|
0010  78 74 2f 70 6c 61 69 6e  3b 20 63 68 61 72 73 65  |xt/plain; charse|
0020  74 3d 75 74 66 2d 38 05  00 02 32 30 06 00 1d 53  |t=utf-8...20...S|
0030  61 74 2c 20 32 36 20 53  65 70 20 32 30 32 36 20  |at, 26 Sep 2026 |
0040  31 31 3a 32 32 3a 35 30  20 47 4d 54 07 00 0a 62  |11:22:50 GMT...b|
0050  73 65 72 76 65 2f 31 2e  30 09 00 1d 53 61 74 2c  |serve/1.0...Sat,|
0060  20 32 36 20 53 65 70 20  32 30 32 36 20 31 31 3a  | 26 Sep 2026 11:|
0070  32 32 3a 34 39 20 47 4d  54 0a 00 15 22 31 38 64  |22:49 GMT...\"18d|
0080  38 64 62 31 64 31 34 33  30 65 61 35 31 2d 31 34  |8db1d1430ea51-14|
0090  22                                                |\"|
```

### 3.1 Frame header (0x00–0x07)

| Offset | Octets | Field | Value |
|---|---|---|---|
| 0x00 | `00 00 89` | `Length` | 137 |
| 0x03 | `02` | `Type` | `RESPONSE` |
| 0x04 | `00` | `Flags` | **no `END_MESSAGE`** — the message continues, so at least one `DATA` frame follows |
| 0x05 | `00 00 01` | `R` + `Stream ID` | stream 1: the same stream the request arrived on |

### 3.2 Response payload (0x08–0x90)

| Offset | Octets | Meaning |
|---|---|---|
| 0x08 | `00 c8` | `status` = 200 |
| 0x0a | `06` | header block: 6 entries |
| 0x0b | `04` `00 19` `74 65 ... 38` | index 4 → `content-type`, len 25, `text/plain; charset=utf-8` |
| 0x27 | `05` `00 02` `32 30` | index 5 → `content-length`, len 2, `20` |
| 0x2c | `06` `00 1d` `53 61 ... 54` | index 6 → `date`, len 29, `Sat, 26 Sep 2026 11:22:50 GMT` |
| 0x4c | `07` `00 0a` `62 73 ... 30` | index 7 → `server`, len 10, `bserve/1.0` |
| 0x59 | `09` `00 1d` `53 61 ... 54` | index 9 → `last-modified`, len 29 |
| 0x79 | `0a` `00 15` `22 31 ... 22` | index 10 → `etag`, len 21, `"18d8db1d1430ea51-14"` |

`content-length: 20` is here, and it is **advisory** (SPEC 8.6). The client
reads it, compares it to what the frames actually carried, and would warn on a
mismatch — but it never uses it to decide where the body ends. The frames
decide. This is the single largest difference from the protocol it replaces:
HTTP/1.1 has two mechanisms competing to answer "where does this end", and
that disagreement is what request smuggling is made of.

---

## 4. DATA frame — 8 + 20 octets, server to client (SPEC 7.3)

```
0000  00 00 14 03 01 00 00 01  68 65 6c 6c 6f 2c 20 66  |........hello, f|
0010  72 61 6d 65 64 20 77 6f  72 6c 64 0a              |ramed world.|
```

| Offset | Octets | Field | Value |
|---|---|---|---|
| 0x00 | `00 00 14` | `Length` | 20 |
| 0x03 | `03` | `Type` | `DATA` |
| 0x04 | `01` | `Flags` | `END_MESSAGE` — the message is complete |
| 0x05 | `00 00 01` | `R` + `Stream ID` | stream 1 |
| 0x08 | `68 65 ... 0a` | payload | `hello, framed world\n` |

Twenty octets of body, and the client stops reading the message here because
the flag said so, not because the socket closed. **The connection is still
open.** The next request would begin at the very next octet, on stream 3.

---

## 5. What the whole exchange cost

| | Octets |
|---|---|
| Preface (once per connection) | 8 |
| Request: header + payload | 8 + 49 = 57 |
| Response: header + payload | 8 + 137 = 145 |
| Body: header + payload | 8 + 20 = 28 |
| **Total after the preface** | **230**, of which 20 are the file |

The same fetch in HTTP/1.1, carrying the same header fields with the same
values, is 317 octets: an 85-octet request, a 212-octet response head, and the
same 20-octet body. So BHTTP/1 spends 230 where HTTP/1.1 spends 317, and the
parser never scans for a delimiter.

The 27% saving is not the point, though — the point is the 24 octets of frame
headers. Three times in this exchange, a receiver knew exactly how many octets
to read before it knew what it was reading. That is what lets the connection
survive a frame it does not understand (SPEC 10), a frame it cannot parse
(SPEC 9.1), and a body that happens to contain something that looks exactly
like a request.

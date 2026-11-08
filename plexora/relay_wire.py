"""The relay's end-to-end frames: the reference both ends are checked against.

A page on the website and a data node on a cluster talk through a relay
(docs/internal/WEB_REMOTE_RELAY.md) that forwards bytes and never reads them.
Every frame between the two is sealed with AES-256-GCM under a key the page
made and handed to the node over SSH; this module is what a frame is, in both
directions, and the browser client is held to it byte for byte through the
vectors in tests/fixtures/relay_wire_vectors.json (tools/relay_wire_vectors.py
writes them).

    sealed    = nonce(12) || AES-256-GCM(K, nonce, plaintext, aad)
    aad       = "plexora-relay.v1|" + room + "|" + dir      (dir: p2n or n2p)
    plaintext = kind(u8) || stream(u32 BE) || hlen(u32 BE) || header || body

`header` is a JSON object in UTF-8, written compactly and without escaping
what does not need it -- exactly what `JSON.stringify` writes, so a page and a
node encoding the same header produce the same bytes. Decoders accept any
valid JSON object.

The relay itself adds one thing outside the seal: the 8-byte pid of the page a
frame is from or for (`with_pid`, `split_pid`), all zeros meaning every page.

Pure and stdlib apart from `cryptography`'s AESGCM, so the website's Python,
the node and the tests import it without Flask or a network stack.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import re
import secrets
import struct
from typing import NamedTuple

#: The WebSocket subprotocol both ends offer, and the AAD's prefix.
PROTOCOL = "plexora-relay.v1"
#: The `v` in HELLO headers and in the relay file.
VERSION = 1

HELLO = 1
REQUEST = 2
REQUEST_BODY = 3
RESPONSE_HEAD = 4
RESPONSE_BODY = 5
CANCEL = 6
STOP = 7

KIND_NAMES = {
    HELLO: "HELLO",
    REQUEST: "REQUEST",
    REQUEST_BODY: "REQUEST_BODY",
    RESPONSE_HEAD: "RESPONSE_HEAD",
    RESPONSE_BODY: "RESPONSE_BODY",
    CANCEL: "CANCEL",
    STOP: "STOP",
}

#: Page to node, and node to page. Part of the AAD, so a frame replayed in
#: the other direction does not open.
P2N = "p2n"
N2P = "n2p"
DIRECTIONS = (P2N, N2P)

KEY_SIZE = 32
NONCE_SIZE = 12
TAG_SIZE = 16
PID_SIZE = 8
#: kind(u8) + stream(u32) + hlen(u32).
PREFIX_SIZE = 9
#: Every page, as a node addresses it.
ALL = bytes(PID_SIZE)

#: The largest body a sender puts in one frame. A RESPONSE_BODY frame of this
#: much body, with its pid, nonce, tag, prefix and `{"end":false}`, is 58
#: bytes over: inside the relay's "1 MiB + 64 bytes".
MAX_CHUNK = 1 << 20
MAX_FRAME = MAX_CHUNK + 64

MAX_STREAM = 0xFFFFFFFF

#: What a room id may be (the relay refuses anything else).
ROOM_RE = re.compile(r"^[A-Za-z0-9_-]{22,64}$")
#: A room auth: 32 random bytes in unpadded base64url.
AUTH_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]*$")
_PREFIX = struct.Struct(">BII")


class RelayWireError(ValueError):
    """Something that is not a frame of this protocol."""


class SealError(RelayWireError):
    """A sealed frame that does not open: wrong key, room or direction,
    tampered with, or too short to be one."""


class FrameError(RelayWireError):
    """A plaintext that is not a well-formed frame."""


class Frame(NamedTuple):
    kind: int
    stream: int
    header: dict
    body: bytes


# -- sealing ----------------------------------------------------------------


def aad(room, direction):
    """The associated data a frame between `room`'s two ends is sealed with."""
    if direction not in DIRECTIONS:
        raise RelayWireError(f"direction must be {P2N!r} or {N2P!r}, not {direction!r}")
    return f"{PROTOCOL}|{room}|{direction}".encode("utf-8")


def _aead(key):
    if not isinstance(key, (bytes, bytearray)) or len(key) != KEY_SIZE:
        raise RelayWireError(f"the key must be {KEY_SIZE} bytes")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(bytes(key))


def seal(key, room, direction, plaintext, nonce=None):
    """`nonce || AES-256-GCM(key, nonce, plaintext, aad(room, direction))`.

    `nonce` is for the vectors only: every real frame takes a fresh random
    one, and a nonce used twice under one key gives the key away.
    """
    if nonce is None:
        nonce = os.urandom(NONCE_SIZE)
    elif len(nonce) != NONCE_SIZE:
        raise RelayWireError(f"the nonce must be {NONCE_SIZE} bytes")
    nonce = bytes(nonce)
    return nonce + _aead(key).encrypt(nonce, bytes(plaintext), aad(room, direction))


def open_(key, room, direction, sealed):
    """The plaintext of a sealed frame, or SealError."""
    aead = _aead(key)
    associated = aad(room, direction)
    sealed = bytes(sealed)
    if len(sealed) < NONCE_SIZE + TAG_SIZE:
        raise SealError("too short to be a sealed frame")
    from cryptography.exceptions import InvalidTag

    try:
        return aead.decrypt(sealed[:NONCE_SIZE], sealed[NONCE_SIZE:], associated)
    except InvalidTag:
        raise SealError("the frame does not open with this key, room and "
                        "direction") from None


# -- framing ----------------------------------------------------------------


def header_bytes(header):
    """A header as the wire carries it: compact JSON, UTF-8, unescaped
    non-ASCII -- `JSON.stringify`'s output for the same object."""
    if not isinstance(header, dict):
        raise FrameError("a frame header is a JSON object")
    try:
        return json.dumps(header, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FrameError(f"the header cannot be written as JSON: {exc}") from None


def encode(kind, stream, header, body=b""):
    """`kind || stream || hlen || header || body`, unsealed."""
    if not isinstance(kind, int) or not 0 <= kind <= 0xFF:
        raise FrameError(f"kind must be a u8, not {kind!r}")
    if not isinstance(stream, int) or not 0 <= stream <= MAX_STREAM:
        raise FrameError(f"stream must be a u32, not {stream!r}")
    head = header_bytes(header)
    return _PREFIX.pack(kind, stream, len(head)) + head + bytes(body)


def decode(data):
    """A Frame from a plaintext, or FrameError. Kinds this module does not
    name are returned as they are, for the caller to ignore."""
    data = bytes(data)
    if len(data) < PREFIX_SIZE:
        raise FrameError("too short to be a frame")
    kind, stream, hlen = _PREFIX.unpack_from(data)
    if hlen > len(data) - PREFIX_SIZE:
        raise FrameError("the header runs past the end of the frame")
    raw = data[PREFIX_SIZE:PREFIX_SIZE + hlen]
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameError(f"the header is not JSON: {exc}") from None
    if not isinstance(header, dict):
        raise FrameError("the header is not a JSON object")
    return Frame(kind, stream, header, data[PREFIX_SIZE + hlen:])


def seal_frame(key, room, direction, kind, stream, header, body=b"", nonce=None):
    """`seal(encode(...))`."""
    return seal(key, room, direction, encode(kind, stream, header, body), nonce=nonce)


def open_frame(key, room, direction, sealed):
    """`decode(open_(...))`."""
    return decode(open_(key, room, direction, sealed))


# -- pids -------------------------------------------------------------------


def with_pid(pid, payload):
    """`pid || payload`, as a node sends a frame and the relay hands one over."""
    pid = bytes(pid)
    if len(pid) != PID_SIZE:
        raise RelayWireError(f"a pid is {PID_SIZE} bytes")
    return pid + bytes(payload)


def split_pid(data):
    """`(pid, payload)` from a relayed message."""
    data = bytes(data)
    if len(data) < PID_SIZE:
        raise RelayWireError("too short to carry a pid")
    return data[:PID_SIZE], data[PID_SIZE:]


def pid_hex(pid):
    """A pid as the relay's tags spell it: 16 lowercase hex digits."""
    return bytes(pid).hex()


def pid_from_hex(text):
    try:
        pid = bytes.fromhex(text)
    except (TypeError, ValueError):
        raise RelayWireError(f"not a pid: {text!r}") from None
    if len(pid) != PID_SIZE:
        raise RelayWireError(f"not a pid: {text!r}")
    return pid


# -- base64url, keys, rooms ---------------------------------------------------


def b64url_encode(data):
    """Unpadded base64url."""
    return base64.urlsafe_b64encode(bytes(data)).rstrip(b"=").decode("ascii")


def b64url_decode(text):
    """Bytes from base64url, padded or not; RelayWireError on anything else
    (including standard base64's `+` and `/`)."""
    if not isinstance(text, str) or not _B64URL_RE.match(text.rstrip("=")):
        raise RelayWireError("not base64url")
    stripped = text.rstrip("=")
    if len(stripped) % 4 == 1:
        raise RelayWireError("not base64url")
    try:
        return base64.urlsafe_b64decode(stripped + "=" * (-len(stripped) % 4))
    except (binascii.Error, ValueError):
        raise RelayWireError("not base64url") from None


def key_to_b64(key):
    if len(key) != KEY_SIZE:
        raise RelayWireError(f"the key must be {KEY_SIZE} bytes")
    return b64url_encode(key)


def key_from_b64(text):
    """The 32-byte key from its base64url form, or RelayWireError."""
    key = b64url_decode(text)
    if len(key) != KEY_SIZE:
        raise RelayWireError(f"the key must be {KEY_SIZE} bytes, not {len(key)}")
    return key


def new_key():
    """A fresh 32-byte frame key."""
    return secrets.token_bytes(KEY_SIZE)


def new_room():
    """A fresh room id: 16 random bytes, 22 characters of base64url."""
    return b64url_encode(secrets.token_bytes(16))


def new_auth():
    """A fresh room auth: 32 random bytes, 43 characters of base64url."""
    return b64url_encode(secrets.token_bytes(32))


def check_room(room):
    """`room` if the relay would take it as a room id, else RelayWireError."""
    if not isinstance(room, str) or not ROOM_RE.match(room):
        raise RelayWireError("a room id is 22 to 64 characters of A-Z, a-z, 0-9, _ and -")
    return room


def check_auth(auth):
    """`auth` if it is 43 characters of base64url (32 bytes), else RelayWireError."""
    if not isinstance(auth, str) or not AUTH_RE.match(auth):
        raise RelayWireError("a room auth is 43 characters of base64url (32 bytes)")
    b64url_decode(auth)
    return auth


def same_auth(a, b):
    """Constant-time comparison of two auths (or their hashes)."""
    return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))


def subprotocols(auth, licence=None):
    """What a socket offers in `Sec-WebSocket-Protocol`: the protocol, the
    room auth, and (pages only) a licence certificate."""
    offered = [PROTOCOL, f"a.{check_auth(auth)}"]
    if licence:
        offered.append(f"l.{licence}")
    return offered


def parse_presence(text):
    """`{"node": bool, "pages": int}` from a relay presence text frame, or
    None for any other text."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("t") != "presence":
        return None
    pages = data.get("pages")
    if isinstance(pages, bool) or not isinstance(pages, int) or pages < 0:
        pages = 0
    return {"node": data.get("node") is True, "pages": pages}

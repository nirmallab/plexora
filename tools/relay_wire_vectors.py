#!/usr/bin/env python
"""Write the relay's frame vectors, for a second implementation to match.

    python tools/relay_wire_vectors.py           # rewrite the vectors file
    python tools/relay_wire_vectors.py --check   # exit 1 when it is stale

Every value is fixed -- key, room, nonces, pids -- so the file is the same on
every run, and a browser client (or any other) can seal each frame itself and
compare bytes with `sealed_hex`, open each `sealed_hex` and compare with
`plaintext_hex`, and refuse every entry under `must_fail`. The format is in
docs/internal/WEB_REMOTE_RELAY.md; plexora/relay_wire.py is the reference.
tests/test_relay_wire.py checks the committed file against this script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plexora import relay_wire as wire  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "relay_wire_vectors.json"

KEY = bytes(range(32))
ROOM = "plexora-test-room-0001"
OTHER_ROOM = "plexora-test-room-0002"
# 32 bytes whose base64url uses both of the characters standard base64 spells
# differently ('-' and '_').
AUTH = wire.b64url_encode(bytes([0xFB, 0xFF, 0xBF] * 10 + [0xFE, 0xFF]))
CLIENT = "c-3f2a9d"
STREAM = 0x9E3779B9
NODE_PID = bytes.fromhex("0011223344556677")


def _nonce(index):
    return bytes((index * 16 + n) % 256 for n in range(wire.NONCE_SIZE))


def _frames():
    """(name, direction, kind, stream, header, body), in file order."""
    binary = bytes(range(256))
    return [
        ("hello-page", wire.P2N, wire.HELLO, 0, {"client": CLIENT, "v": 1}, b""),
        ("hello-node", wire.N2P, wire.HELLO, 0,
         {"v": 1, "node_id": "node-ab12cd", "api_version": 1,
          "token": "0123456789abcdef0123456789abcdef", "version": "0.0.30",
          "hostname": "compute-b-16-186", "platform": "linux"}, b""),
        ("request-get", wire.P2N, wire.REQUEST, STREAM,
         {"m": "GET", "p": "/node/v1/hello?x=1&name=caf%C3%A9",
          "h": [["accept", "application/json"]], "client": CLIENT, "end": True}, b""),
        ("request-post-unicode", wire.P2N, wire.REQUEST, STREAM + 1,
         {"m": "POST", "p": "/node/v1/table/cells/columns",
          "h": [["content-type", "application/json; charset=utf-8"],
                ["x-note", "Größe ✓ 細胞 🧫 \"quoted\" back\\slash\nline\ttab\u0001"]],
          "client": CLIENT, "end": False},
         '{"name":"CD4+ T ✓"}'.encode("utf-8")),
        ("request-body-binary", wire.P2N, wire.REQUEST_BODY, STREAM + 1,
         {"end": True}, binary),
        ("response-head", wire.N2P, wire.RESPONSE_HEAD, STREAM,
         {"s": 200, "h": [["Content-Type", "application/octet-stream"],
                          ["X-Plexora-File-Name", "tonsil é.tif"]]}, b""),
        ("response-body-chunk", wire.N2P, wire.RESPONSE_BODY, STREAM,
         {"end": False}, binary * 4),
        ("response-body-end-empty", wire.N2P, wire.RESPONSE_BODY, STREAM,
         {"end": True}, b""),
        ("response-body-error", wire.N2P, wire.RESPONSE_BODY, STREAM + 1,
         {"end": True, "error": "Ошибка: the node's server closed the connection"}, b""),
        ("cancel", wire.P2N, wire.CANCEL, STREAM, {}, b""),
        ("stop", wire.P2N, wire.STOP, 0, {}, b""),
    ]


def build():
    vectors = []
    for index, (name, direction, kind, stream, header, body) in enumerate(_frames()):
        nonce = _nonce(index)
        plaintext = wire.encode(kind, stream, header, body)
        vectors.append({
            "name": name,
            "direction": direction,
            "kind": kind,
            "kind_name": wire.KIND_NAMES[kind],
            "stream": stream,
            "header": header,
            "header_json": wire.header_bytes(header).decode("utf-8"),
            "body_hex": body.hex(),
            "nonce_hex": nonce.hex(),
            "aad": wire.aad(ROOM, direction).decode("utf-8"),
            "plaintext_hex": plaintext.hex(),
            "sealed_hex": wire.seal(KEY, ROOM, direction, plaintext, nonce=nonce).hex(),
        })
    by_name = {v["name"]: v for v in vectors}

    def flipped(hex_text):
        raw = bytearray(bytes.fromhex(hex_text))
        raw[-1] ^= 0x01
        return raw.hex()

    must_fail = [
        {"why": "tampered: the last byte of the tag flipped",
         "key_hex": KEY.hex(), "room": ROOM, "direction": wire.P2N,
         "sealed_hex": flipped(by_name["request-get"]["sealed_hex"])},
        {"why": "opened in the other direction",
         "key_hex": KEY.hex(), "room": ROOM, "direction": wire.N2P,
         "sealed_hex": by_name["hello-page"]["sealed_hex"]},
        {"why": "opened for another room",
         "key_hex": KEY.hex(), "room": OTHER_ROOM, "direction": wire.P2N,
         "sealed_hex": by_name["hello-page"]["sealed_hex"]},
        {"why": "opened with another key",
         "key_hex": bytes(reversed(KEY)).hex(), "room": ROOM, "direction": wire.N2P,
         "sealed_hex": by_name["hello-node"]["sealed_hex"]},
        {"why": "shorter than a nonce and a tag",
         "key_hex": KEY.hex(), "room": ROOM, "direction": wire.P2N,
         "sealed_hex": by_name["cancel"]["sealed_hex"][:2 * (wire.NONCE_SIZE + wire.TAG_SIZE - 1)]},
    ]

    head = by_name["response-head"]["sealed_hex"]
    relayed = [
        {"why": "node to relay: the page's pid, then the sealed frame",
         "pid_hex": NODE_PID.hex(), "payload_hex": head,
         "message_hex": wire.with_pid(NODE_PID, bytes.fromhex(head)).hex()},
        {"why": "node to relay, for every page: the all-zero pid",
         "pid_hex": wire.ALL.hex(), "payload_hex": by_name["hello-node"]["sealed_hex"],
         "message_hex": wire.with_pid(
             wire.ALL, bytes.fromhex(by_name["hello-node"]["sealed_hex"])).hex()},
    ]

    b64url = [{"hex": raw.hex(), "b64url": wire.b64url_encode(raw)}
              for raw in (b"", b"\xfb", b"\xfb\xff", b"\xfb\xff\xbf", KEY,
                          wire.b64url_decode(AUTH))]

    return {
        "about": "Deterministic vectors for the relay's end-to-end frames "
                 "(docs/internal/WEB_REMOTE_RELAY.md). Generated by "
                 "tools/relay_wire_vectors.py; do not edit by hand. "
                 "sealed = nonce(12) || AES-256-GCM(key, nonce, plaintext, aad); "
                 "plaintext = kind(u8) || stream(u32 BE) || hlen(u32 BE) || "
                 "header_json (UTF-8) || body. header_json is what "
                 "JSON.stringify(header) writes.",
        "protocol": wire.PROTOCOL,
        "key_hex": KEY.hex(),
        "key_b64url": wire.key_to_b64(KEY),
        "room": ROOM,
        "auth": AUTH,
        "subprotocols": wire.subprotocols(AUTH),
        "relay_file": {"v": 1, "relay": "wss://relay.plexoraapp.com", "room": ROOM,
                       "auth": AUTH, "key": wire.key_to_b64(KEY), "idle_exit": 1800},
        "max_chunk": wire.MAX_CHUNK,
        "vectors": vectors,
        "must_fail": must_fail,
        "relayed": relayed,
        "b64url": b64url,
    }


def render():
    return json.dumps(build(), indent=2, ensure_ascii=False) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="Exit 1 when the committed file differs from what "
                             "this script writes.")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != text:
            print(f"{args.out} is stale; run tools/relay_wire_vectors.py")
            return 1
        print(f"{args.out} is current")
        return 0
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

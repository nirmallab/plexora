"""The relay's end-to-end frames (plexora/relay_wire.py).

The vectors file is what a second implementation -- the website's browser
client -- is checked against byte for byte, so it must be exactly what this
code writes.
"""

import json
from pathlib import Path

import pytest

from plexora import relay_wire as wire

VECTORS = Path(__file__).parent / "fixtures" / "relay_wire_vectors.json"
KEY = bytes(range(32))
ROOM = "plexora-test-room-0001"


def test_a_frame_round_trips_through_the_seal():
    header = {"m": "POST", "p": "/node/v1/x?y=1", "h": [["x-note", "Größe ✓ 🧫"]],
              "client": "c1", "end": False}
    body = bytes(range(256)) * 3
    sealed = wire.seal_frame(KEY, ROOM, wire.P2N, wire.REQUEST, 0xFFFFFFFF, header, body)
    frame = wire.open_frame(KEY, ROOM, wire.P2N, sealed)
    assert frame == wire.Frame(wire.REQUEST, 0xFFFFFFFF, header, body)
    # A fresh nonce every time: the same frame never seals the same way twice.
    assert sealed != wire.seal_frame(KEY, ROOM, wire.P2N, wire.REQUEST, 0xFFFFFFFF,
                                     header, body)
    assert len(sealed) == (wire.NONCE_SIZE + wire.TAG_SIZE + wire.PREFIX_SIZE
                           + len(wire.header_bytes(header)) + len(body))


def test_a_full_chunk_fits_the_relay_s_frame_limit():
    sealed = wire.seal_frame(KEY, ROOM, wire.N2P, wire.RESPONSE_BODY, 7, {"end": False},
                             bytes(wire.MAX_CHUNK))
    assert len(wire.with_pid(bytes(8), sealed)) <= wire.MAX_FRAME


@pytest.mark.parametrize("change", ["direction", "room", "key", "tamper", "short"])
def test_a_frame_opens_only_where_it_was_sealed_for(change):
    sealed = bytearray(wire.seal_frame(KEY, ROOM, wire.P2N, wire.HELLO, 0,
                                       {"client": "c1", "v": 1}))
    key, room, direction = KEY, ROOM, wire.P2N
    if change == "direction":
        direction = wire.N2P
    elif change == "room":
        room = "plexora-test-room-0002"
    elif change == "key":
        key = bytes(reversed(KEY))
    elif change == "tamper":
        sealed[20] ^= 0x40
    else:
        sealed = sealed[:wire.NONCE_SIZE + wire.TAG_SIZE - 1]
    with pytest.raises(wire.SealError):
        wire.open_(key, room, direction, bytes(sealed))
    # A SealError is a RelayWireError is a ValueError: one except catches it.
    assert issubclass(wire.SealError, wire.RelayWireError)
    assert issubclass(wire.RelayWireError, ValueError)


@pytest.mark.parametrize("plaintext", [
    b"",
    b"\x01\x00\x00\x00\x00",
    b"\x01\x00\x00\x00\x00\x00\x00\x00\x10{}",       # hlen past the end
    b"\x01\x00\x00\x00\x00\x00\x00\x00\x02[]",       # not an object
    b"\x01\x00\x00\x00\x00\x00\x00\x00\x02\xff\xfe",  # not UTF-8
])
def test_malformed_plaintexts_are_frame_errors(plaintext):
    with pytest.raises(wire.FrameError):
        wire.decode(plaintext)


def test_encode_refuses_what_the_wire_cannot_carry():
    with pytest.raises(wire.FrameError):
        wire.encode(256, 0, {})
    with pytest.raises(wire.FrameError):
        wire.encode(1, 1 << 32, {})
    with pytest.raises(wire.FrameError):
        wire.encode(1, 0, ["not", "an", "object"])
    with pytest.raises(wire.FrameError):
        wire.encode(1, 0, {"x": float("nan")})
    with pytest.raises(wire.RelayWireError):
        wire.seal(b"short", ROOM, wire.P2N, b"x")
    with pytest.raises(wire.RelayWireError):
        wire.aad(ROOM, "sideways")


def test_headers_are_written_as_json_stringify_writes_them():
    header = {"a": "é ✓ 🧫", "b": [1, True, None], "c": "\"\\\n\u0001/<"}
    assert wire.header_bytes(header) == (
        '{"a":"é ✓ 🧫","b":[1,true,null],"c":"\\"\\\\\\n\\u0001/<"}'.encode("utf-8"))


def test_unknown_kinds_decode_for_the_caller_to_ignore():
    frame = wire.decode(wire.encode(99, 5, {"x": 1}, b"z"))
    assert frame.kind == 99 and frame.body == b"z"


def test_pids():
    pid = bytes.fromhex("0011223344556677")
    message = wire.with_pid(pid, b"payload")
    assert wire.split_pid(message) == (pid, b"payload")
    assert wire.pid_hex(pid) == "0011223344556677"
    assert wire.pid_from_hex("0011223344556677") == pid
    assert wire.ALL == bytes(8)
    for bad in ("00", "zz" * 8, None):
        with pytest.raises(wire.RelayWireError):
            wire.pid_from_hex(bad)
    with pytest.raises(wire.RelayWireError):
        wire.with_pid(b"\x00", b"x")
    with pytest.raises(wire.RelayWireError):
        wire.split_pid(b"\x00" * 7)


def test_keys_rooms_and_auths():
    key = wire.new_key()
    assert len(key) == 32 and wire.key_from_b64(wire.key_to_b64(key)) == key
    assert "=" not in wire.key_to_b64(key)
    room, auth = wire.new_room(), wire.new_auth()
    assert wire.check_room(room) == room and len(room) == 22
    assert wire.check_auth(auth) == auth and len(auth) == 43
    assert len(wire.b64url_decode(auth)) == 32
    assert wire.new_room() != room and wire.new_auth() != auth
    # Padded input is accepted; standard base64's alphabet is not.
    assert wire.b64url_decode("-_8=") == wire.b64url_decode("-_8") == b"\xfb\xff"
    for bad in ("+/8", "a", "ab$c", None):
        with pytest.raises(wire.RelayWireError):
            wire.b64url_decode(bad)
    with pytest.raises(wire.RelayWireError):
        wire.key_from_b64(wire.b64url_encode(bytes(31)))
    for bad in ("short", "x" * 65, "has space in it xxxxxxxx", None):
        with pytest.raises(wire.RelayWireError):
            wire.check_room(bad)
    with pytest.raises(wire.RelayWireError):
        wire.check_auth(auth[:-1])
    assert wire.subprotocols(auth) == ["plexora-relay.v1", f"a.{auth}"]
    assert wire.subprotocols(auth, "CERT")[-1] == "l.CERT"
    assert wire.same_auth(auth, auth) and not wire.same_auth(auth, room)


def test_presence():
    assert wire.parse_presence('{"t":"presence","node":true,"pages":2}') == {
        "node": True, "pages": 2}
    assert wire.parse_presence('{"t":"presence","node":false,"pages":0}') == {
        "node": False, "pages": 0}
    assert wire.parse_presence("pong") is None
    assert wire.parse_presence('{"t":"other"}') is None


# -- the vectors ---------------------------------------------------------------


def test_the_vectors_file_is_what_the_generator_writes():
    from tools import relay_wire_vectors

    assert VECTORS.read_text(encoding="utf-8") == relay_wire_vectors.render(), (
        "tests/fixtures/relay_wire_vectors.json is stale: run "
        "python tools/relay_wire_vectors.py")


def test_every_vector_seals_and_opens_as_recorded():
    data = json.loads(VECTORS.read_text(encoding="utf-8"))
    key = bytes.fromhex(data["key_hex"])
    assert wire.key_from_b64(data["key_b64url"]) == key
    room = data["room"]
    assert {v["kind"] for v in data["vectors"]} == set(wire.KIND_NAMES)
    for vector in data["vectors"]:
        body = bytes.fromhex(vector["body_hex"])
        plaintext = wire.encode(vector["kind"], vector["stream"], vector["header"], body)
        assert plaintext.hex() == vector["plaintext_hex"], vector["name"]
        assert wire.header_bytes(vector["header"]).decode("utf-8") == vector["header_json"]
        sealed = wire.seal(key, room, vector["direction"], plaintext,
                           nonce=bytes.fromhex(vector["nonce_hex"]))
        assert sealed.hex() == vector["sealed_hex"], vector["name"]
        frame = wire.open_frame(key, room, vector["direction"], bytes.fromhex(vector["sealed_hex"]))
        assert frame == (vector["kind"], vector["stream"], vector["header"], body)
    for case in data["must_fail"]:
        with pytest.raises(wire.SealError):
            wire.open_(bytes.fromhex(case["key_hex"]), case["room"], case["direction"],
                       bytes.fromhex(case["sealed_hex"]))
    for case in data["relayed"]:
        assert wire.split_pid(bytes.fromhex(case["message_hex"])) == (
            bytes.fromhex(case["pid_hex"]), bytes.fromhex(case["payload_hex"]))
    for case in data["b64url"]:
        assert wire.b64url_encode(bytes.fromhex(case["hex"])) == case["b64url"]
        assert wire.b64url_decode(case["b64url"]).hex() == case["hex"]

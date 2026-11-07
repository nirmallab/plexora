"""The built extension loads and answers, on every platform cibuildwheel builds.

Small known answers only: Plexora's own suite (tests/test_kernels_package.py
there) holds every port to numba and to the Python it ports, on the same
inputs, and needs Plexora installed; this needs nothing but numpy.
"""

import numpy as np
import pytest

import plexora_kernels as pk

K = "plexora.server."


def test_the_registry_names_every_port():
    assert pk.API == 1
    assert len(pk.KERNELS) == 22
    assert set(pk.FINGERPRINTS) == {key.rsplit(".", 1)[0] for key in pk.KERNELS}
    for module, names in pk.FINGERPRINTS.items():
        for name in names:
            assert f"{module}.{name}" in pk.KERNELS


def _lzw_encode(data):
    out, acc, nbits = bytearray(), 0, 0

    def put(code, width):
        nonlocal acc, nbits
        acc, nbits = (acc << width) | code, nbits + width
        while nbits >= 8:
            nbits -= 8
            out.append((acc >> nbits) & 0xFF)
            acc &= (1 << nbits) - 1

    table, width, nxt, w = {bytes((i,)): i for i in range(256)}, 9, 258, b""
    put(256, width)
    for value in data:
        wc = w + bytes((value,))
        if wc in table:
            w = wc
            continue
        put(table[w], width)
        table[wc], nxt = nxt, nxt + 1
        if nxt >= (1 << width) and width < 12:
            width += 1
        if nxt >= 4094:
            put(256, width)
            table, width, nxt = {bytes((i,)): i for i in range(256)}, 9, 258
        w = bytes((value,))
    if w:
        put(table[w], width)
        nxt += 1
        if nxt >= (1 << width) and width < 12:
            width += 1
    put(257, width)
    if nbits:
        out.append((acc << (8 - nbits)) & 0xFF)
    return bytes(out)


@pytest.mark.parametrize("data", [b"", b"a", b"abababababab", bytes(range(256)) * 40,
                                  np.random.default_rng(0).integers(0, 256, 50_000,
                                                                    dtype=np.uint8).tobytes()],
                         ids=["empty", "one", "repeats", "ramp", "noise"])
def test_lzw_round_trips(data):
    assert pk.lzw_decode(_lzw_encode(data)) == data
    assert pk.lzw_decode(np.frombuffer(_lzw_encode(data), np.uint8)) == data


def test_old_style_lzw_is_refused():
    with pytest.raises(NotImplementedError):
        pk.lzw_decode(b"\x00\x01\x02")


def test_outlines_match_shifted_comparisons():
    rng = np.random.default_rng(1)
    labels = rng.integers(0, 5, (30, 41)).astype(np.int32)
    out = np.zeros(labels.shape, bool)
    pk.KERNELS[K + "utils.label_kernels._boundary"](labels, 1, out)
    padded = np.pad(labels, 1, constant_values=-1)
    expect = np.zeros(labels.shape, bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            other = padded[1 + dy:31 + dy, 1 + dx:42 + dx]
            expect |= (other != labels) & (other != -1)
    assert np.array_equal(out, expect & (labels != 0))


def test_one_voxel_is_a_closed_cube():
    data = np.zeros((3, 3, 3), np.uint32)
    data[1, 1, 1] = 4
    skip = np.zeros(2, bool)
    count = pk.KERNELS[K + "surface.nets._count"]
    assert tuple(count(data, skip)) == (8, 6)


def test_a_tetrahedron_has_its_volume():
    p = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], np.float64)
    t = np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]], np.int64)
    out = pk.KERNELS[K + "surface.mesh._signed_volumes"](
        p, t, np.array([0], np.int64), np.array([4], np.int64), np.ones(3), np.zeros(1))
    assert out[0] == pytest.approx(1 / 6)


def test_a_call_no_port_takes_and_no_fallback_says_so():
    with pytest.raises(TypeError, match="no compiled form"):
        pk.KERNELS[K + "utils.label_kernels._boundary"](np.zeros((2, 2)), 1, np.zeros((2, 2), bool))

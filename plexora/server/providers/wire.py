"""How an array crosses between a node and the primary.

Two shapes, and the choice between them is the whole content of this module.

**A frame**: four bytes of length, then a JSON header, then raw array bytes.
Used wherever the answer is numbers -- a column of intensities, a packed set of
filter columns, a region of pixels. Raw rather than base64 because these are
megabytes at a time and base64 is a third more of them for no benefit, and
length-prefixed rather than header-carried because the metadata includes things
that do not belong in an HTTP header: a categorical column's level order can
run to hundreds of strings.

**Plain JSON**: everything small, and everything whose values are strings. A
text annotation column is not a buffer in any language, and pretending it is
would mean shipping numpy's object dtype -- which only round-trips through
pickle, which is not something to accept off a network.

`dtype` travels with the bytes and is applied on arrival rather than assumed:
the one failure this makes impossible is the quiet one, where both ends agree
on a length and disagree on a width.
"""

from __future__ import annotations

import json
import struct

import numpy as np

#: Content type for a framed response. Deliberately not application/json: a
#: proxy or a browser that sniffed one would try to parse the array bytes.
CONTENT_TYPE = "application/vnd.plexora.frame"

_HEADER_STRUCT = struct.Struct(">I")

#: Refused rather than allocated. A header this large is a bug or an attack;
#: the real ones are a few kilobytes even with a long category list.
MAX_HEADER_BYTES = 4 * 1024 * 1024


def pack(meta: dict, payload: bytes = b"") -> bytes:
    """One framed message: length, JSON header, raw bytes."""
    header = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    return _HEADER_STRUCT.pack(len(header)) + header + bytes(payload)


def unpack(data: bytes) -> tuple[dict, bytes]:
    """(meta, payload) from a framed message."""
    if len(data) < _HEADER_STRUCT.size:
        raise ValueError("truncated frame: no header length")
    (size,) = _HEADER_STRUCT.unpack_from(data, 0)
    if size > MAX_HEADER_BYTES:
        raise ValueError(f"frame header claims {size} bytes")
    start = _HEADER_STRUCT.size
    end = start + size
    if len(data) < end:
        raise ValueError("truncated frame: header shorter than declared")
    meta = json.loads(data[start:end].decode("utf-8"))
    return meta, data[end:]


def pack_array(array, **meta) -> bytes:
    """One numpy array as a frame, or as JSON when it is not numbers.

    The `kind` field says which happened, so the reader does not have to infer
    it from a dtype string -- and so a column of strings and a column of floats
    come back through one call at both ends.
    """
    array = np.asarray(array)
    if array.dtype.kind in "biufc":
        contiguous = np.ascontiguousarray(array)
        return pack({
            **meta,
            "kind": "array",
            "dtype": contiguous.dtype.str,
            "shape": list(contiguous.shape),
        }, contiguous.tobytes())
    # Strings, objects, datetimes: JSON, because the alternative is numpy's
    # object dtype, which only survives a round trip through pickle.
    return pack({
        **meta,
        "kind": "json",
        "values": [None if value is None else str(value) for value in array.tolist()],
    })


def unpack_array(data: bytes) -> tuple[np.ndarray, dict]:
    """(array, meta) from what `pack_array` produced."""
    meta, payload = unpack(data)
    if meta.get("kind") == "json":
        return np.array(meta.get("values") or [], dtype=object), meta
    array = np.frombuffer(payload, dtype=np.dtype(meta["dtype"]))
    shape = tuple(meta.get("shape") or (array.size,))
    if int(np.prod(shape)) != array.size:
        raise ValueError(
            f"frame declares shape {shape} but carries {array.size} values")
    return array.reshape(shape), meta


def pack_columns(columns: dict) -> bytes:
    """A {name: float32 array} set as one frame.

    One message rather than one per column, because the caller asks for a set
    -- `get_filter_columns` is given every marker a gate names at once -- and a
    request per column would multiply the round trip by the number of sliders.
    """
    names = list(columns)
    if not names:
        return pack({"kind": "columns", "names": [], "length": 0, "dtype": "<f4"})
    arrays = [np.ascontiguousarray(columns[name], dtype=np.float32) for name in names]
    length = int(arrays[0].size)
    return pack(
        {"kind": "columns", "names": names, "length": length, "dtype": "<f4"},
        b"".join(array.tobytes() for array in arrays),
    )


def unpack_columns(data: bytes) -> dict:
    meta, payload = unpack(data)
    names = meta.get("names") or []
    if not names:
        return {}
    length = int(meta["length"])
    dtype = np.dtype(meta.get("dtype") or "<f4")
    flat = np.frombuffer(payload, dtype=dtype)
    expected = length * len(names)
    if flat.size != expected:
        raise ValueError(
            f"frame declares {len(names)} columns of {length} but carries {flat.size} values")
    return {name: flat[i * length:(i + 1) * length]
            for i, name in enumerate(names)}

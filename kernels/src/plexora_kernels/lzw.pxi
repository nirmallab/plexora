# TIFF's LZW, decoded. Included by _kernels.pyx.
#
# A port of plexora/browser/imagecodecs_shim.py `lzw_decode` (TIFF 6: codes
# MSB-first, 9 to 12 bits, early change), which stands in for
# imagecodecs.lzw_decode on the website, where imagecodecs has no build.

from cpython.bytes cimport PyBytes_FromStringAndSize

cdef enum:
    _CLEAR = 256
    _EOI = 257
    _TABLE = 4096


cdef int _lzw(const uint8_t* data, Py_ssize_t size, uint8_t** result, Py_ssize_t* length,
              Py_ssize_t capacity) noexcept nogil:
    """0, or -1 for no memory, -2 for a code no table entry has."""
    # On the heap: WebAssembly's stack is small.
    cdef int32_t* prefix = <int32_t*>malloc(_TABLE * (2 * sizeof(int32_t) + 2))
    if prefix == NULL:
        return -1
    cdef int32_t* size_of = prefix + _TABLE
    cdef uint8_t* suffix = <uint8_t*>(size_of + _TABLE)
    cdef uint8_t* first = suffix + _TABLE
    cdef int64_t total = <int64_t>size * 8, position = 0
    cdef int width = 9, nxt = 258, code, previous = -1, entry_len, walk, i
    cdef Py_ssize_t at, pos = 0
    cdef uint32_t word
    cdef uint8_t b0, b1, b2, head
    cdef uint8_t* out
    cdef uint8_t* grown
    for i in range(256):
        prefix[i] = -1
        suffix[i] = <uint8_t>i
        first[i] = <uint8_t>i
        size_of[i] = 1
    size_of[_CLEAR] = 0
    size_of[_EOI] = 0
    if capacity < 4096:
        capacity = 4096
    out = <uint8_t*>malloc(capacity)
    if out == NULL:
        free(prefix)
        return -1
    while position + width <= total:
        at = <Py_ssize_t>(position >> 3)
        b0 = data[at]
        b1 = data[at + 1] if at + 1 < size else 0
        b2 = data[at + 2] if at + 2 < size else 0
        word = (<uint32_t>b0 << 16) | (<uint32_t>b1 << 8) | b2
        code = <int>((word >> (24 - (position & 7) - width)) & ((1 << width) - 1))
        position += width
        if code == _EOI:
            break
        if code == _CLEAR:
            nxt = 258
            width = 9
            previous = -1
            continue
        if previous < 0:
            if code >= 256:
                free(out)
                free(prefix)
                return -2
            entry_len = 1
            head = first[code]
            walk = code
        elif code < nxt:
            entry_len = size_of[code]
            head = first[code]
            walk = code
            if nxt < _TABLE:
                prefix[nxt] = previous
                suffix[nxt] = first[code]
                first[nxt] = first[previous]
                size_of[nxt] = size_of[previous] + 1
                nxt += 1
        else:
            # The code being defined: previous + previous's first byte.
            entry_len = size_of[previous] + 1
            head = first[previous]
            walk = -1
            if nxt < _TABLE:
                prefix[nxt] = previous
                suffix[nxt] = first[previous]
                first[nxt] = first[previous]
                size_of[nxt] = entry_len
                walk = nxt
                nxt += 1
        if pos + entry_len > capacity:
            while pos + entry_len > capacity:
                capacity *= 2
            grown = <uint8_t*>realloc(out, capacity)
            if grown == NULL:
                free(out)
                free(prefix)
                return -1
            out = grown
        if walk >= 0:
            i = entry_len - 1
            while walk >= 0 and i >= 0:
                out[pos + i] = suffix[walk]
                walk = prefix[walk]
                i -= 1
        else:
            # The table is full and the stream names the next code anyway:
            # previous's bytes, then its first byte (the shim's reading).
            i = size_of[previous] - 1
            walk = previous
            while walk >= 0 and i >= 0:
                out[pos + i] = suffix[walk]
                walk = prefix[walk]
                i -= 1
            out[pos + entry_len - 1] = head
        pos += entry_len
        previous = code if code < nxt else nxt - 1
        if nxt >= (1 << width) - 1 and width < 12:
            width += 1
    free(prefix)
    result[0] = out
    length[0] = pos
    return 0


def lzw_decode(data, buffersize=0, out=None):
    """TIFF's LZW, as imagecodecs.lzw_decode: the decoded bytes. The pre-TIFF
    6 variant (LSB-first) is refused, as by libtiff without its old-LZW
    support."""
    cdef const uint8_t[::1] view
    cdef uint8_t* decoded = NULL
    cdef Py_ssize_t length = 0, size, capacity
    cdef int status
    try:
        view = data
    except (TypeError, ValueError, BufferError):
        view = bytes(data)
    size = view.shape[0]
    if size >= 2 and view[0] == 0 and view[1] == 1:
        raise NotImplementedError("old-style (pre-TIFF 6) LZW is not supported")
    if size == 0:
        return b""
    capacity = buffersize if buffersize is not None and buffersize > 0 else size * 4
    with nogil:
        status = _lzw(&view[0], size, &decoded, &length, capacity)
    if status == -1:
        raise MemoryError("LZW: out of memory")
    if status == -2:
        raise RuntimeError("LZW: the stream names a code before any string")
    try:
        return PyBytes_FromStringAndSize(<char*>decoded, length)
    finally:
        free(decoded)

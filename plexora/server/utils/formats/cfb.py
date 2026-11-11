"""Microsoft Compound File Binary (OLE2 structured storage), read in place.

The container of Zeiss ZVI (and of Olympus OIB, old Office files, ...): a
small file system inside one file. Written from Microsoft's public
specification [MS-CFB]; standard library only, so it runs on the website too.

- The header (the first 512 bytes): the signature D0 CF 11 E0 A1 B1 1A E1,
  0xFFFE at 28, the sector size as a power of two at 30 (9, 512-byte sectors,
  in version 3; 12, 4096, in version 4), the mini sector size at 32 (6: 64
  bytes), the FAT's sector count at 44, the directory's first sector at 48,
  the mini stream cutoff at 56 (4096), the mini FAT's first sector and sector
  count at 60 and 64, the DIFAT's first sector and count at 68 and 72, and the
  first 109 FAT sector numbers from 76.
- Sector `n` starts at byte `(n + 1) * sector size` (the header is sector -1).
- The FAT is one uint32 per sector: the next sector of the chain the sector is
  in, 0xFFFFFFFE at a chain's end. The FAT's own sectors are listed by the
  header and, past 109 of them, by DIFAT sectors (each a list of FAT sector
  numbers whose last entry is the next DIFAT sector).
- The directory is a chain of 128-byte entries: the UTF-16 name (64 bytes,
  its byte length with the terminator at 64), the type at 66 (1 storage, 2
  stream, 5 root), the left sibling, right sibling and first child at 68, 72
  and 76 (entry numbers: each storage's children are a red-black tree), the
  first sector at 116 and the size at 120 (only its low 32 bits count in
  version 3).
- A stream shorter than the cutoff lives in the mini stream (the root entry's
  own chain), in 64-byte mini sectors chained by the mini FAT.

Nothing is read ahead: opening reads the header, the DIFAT and the directory;
a FAT sector is read the first time a chain passes through it, a stream's
chain is followed only as far as a read reaches, and a read takes only the
sectors holding the bytes asked for, each contiguous run of them in one read.
"""
from __future__ import annotations

import struct
import sys
import threading
from array import array
from typing import Optional

SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MAX_REGULAR = 0xFFFFFFFA        # larger sector numbers are markers (end, free, ...)
_NO_ENTRY = 0xFFFFFFFF

STORAGE, STREAM, ROOT = 1, 2, 5


class CompoundFileError(ValueError):
    """Not a compound file, or one too damaged to read."""


def _uint32s(data: bytes) -> array:
    values = array("I")
    if values.itemsize != 4:     # pragma: no cover -- every platform Python runs on
        values = array("L")
    values.frombytes(data[:len(data) // 4 * 4])
    if sys.byteorder == "big":
        values.byteswap()
    return values


class Entry:
    """One directory entry: a storage (a folder) or a stream (a file)."""

    __slots__ = ("index", "name", "kind", "left", "right", "child", "start", "size",
                 "_children", "_chain", "_chain_ended")

    def __init__(self, index, name, kind, left, right, child, start, size):
        self.index, self.name, self.kind = index, name, kind
        self.left, self.right, self.child = left, right, child
        self.start, self.size = start, size
        self._children: Optional[dict] = None
        self._chain: list = []
        self._chain_ended = False

    @property
    def is_stream(self) -> bool:
        return self.kind == STREAM

    def __repr__(self):
        kind = {STORAGE: "storage", STREAM: "stream", ROOT: "root"}.get(self.kind, self.kind)
        return f"<{kind} {self.name!r} {self.size} bytes>"


class CompoundFile:
    """An open compound file. `handle` is a binary file opened by the caller
    and closed by `close`; every read takes `lock`, so one handle serves any
    number of threads."""

    def __init__(self, handle, lock=None):
        self._handle = handle
        self._lock = lock if lock is not None else threading.RLock()
        with self._lock:
            handle.seek(0, 2)
            self.file_size = handle.tell()
        header = self._read_at(0, 512)
        if len(header) < 512 or header[:8] != SIGNATURE:
            raise CompoundFileError("not a compound file (no OLE2 signature)")
        _minor, self.version, order, sector_shift, mini_shift = \
            struct.unpack_from("<5H", header, 24)
        if order != 0xFFFE or not 7 <= sector_shift <= 16 or not 2 <= mini_shift < sector_shift:
            raise CompoundFileError("the compound file header is not valid")
        (_dir_count, fat_count, dir_start, _txn, self.cutoff, minifat_start, minifat_count,
         difat_start, difat_count) = struct.unpack_from("<9I", header, 40)
        self.sector_size = 1 << sector_shift
        self.mini_size = 1 << mini_shift
        self._per_sector = self.sector_size // 4
        self._fat_sectors = self._difat(header, fat_count, difat_start, difat_count)
        self._fat_pages: dict = {}
        self.entries = self._read_directory(dir_start)
        if not self.entries or self.entries[0].kind != ROOT:
            raise CompoundFileError("the compound file has no root entry")
        self.root = self.entries[0]
        self._minifat = array("I")
        if minifat_count and minifat_start <= _MAX_REGULAR:
            self._minifat = _uint32s(self._read_sectors(self._whole_chain(minifat_start)))

    # -- the file ----------------------------------------------------------------

    def _read_at(self, offset: int, size: int) -> bytes:
        with self._lock:
            self._handle.seek(offset)
            return self._handle.read(size)

    def _sector_offset(self, sector: int) -> int:
        return (sector + 1) * self.sector_size

    def _read_sectors(self, sectors) -> bytes:
        """The sectors' bytes in order (zeros past the end of a cut file)."""
        parts = []
        for first, count in _runs(sectors):
            size = count * self.sector_size
            parts.append(self._read_at(self._sector_offset(first), size).ljust(size, b"\x00"))
        return b"".join(parts)

    # -- the FAT -------------------------------------------------------------------

    def _difat(self, header, fat_count, difat_start, difat_count) -> list:
        """The FAT's sector numbers, in order."""
        sectors = [s for s in struct.unpack_from("<109I", header, 76) if s <= _MAX_REGULAR]
        sector, seen = difat_start, set()
        while sector <= _MAX_REGULAR and sector not in seen and len(seen) <= difat_count:
            seen.add(sector)
            data = self._read_at(self._sector_offset(sector), self.sector_size)
            if len(data) < self.sector_size:
                break
            values = _uint32s(data)
            sectors.extend(v for v in values[:-1] if v <= _MAX_REGULAR)
            sector = values[-1]
        return sectors[:fat_count] if fat_count else sectors

    def _next(self, sector: int) -> int:
        """The FAT's entry for `sector`: the next sector of its chain."""
        page = sector // self._per_sector
        values = self._fat_pages.get(page)
        if values is None:
            if page >= len(self._fat_sectors):
                return _NO_ENTRY
            values = _uint32s(self._read_sectors([self._fat_sectors[page]]))
            self._fat_pages[page] = values
        return values[sector % self._per_sector]

    def _whole_chain(self, start: int) -> list:
        chain, sector, seen = [], start, set()
        while sector <= _MAX_REGULAR and sector not in seen:
            seen.add(sector)
            chain.append(sector)
            sector = self._next(sector)
        return chain

    # -- the directory ---------------------------------------------------------------

    def _read_directory(self, start) -> list:
        data = self._read_sectors(self._whole_chain(start))
        entries = []
        for index in range(len(data) // 128):
            raw = data[index * 128:(index + 1) * 128]
            length = struct.unpack_from("<H", raw, 64)[0]
            left, right, child = struct.unpack_from("<3I", raw, 68)
            start_sector = struct.unpack_from("<I", raw, 116)[0]
            size = struct.unpack_from("<Q", raw, 120)[0]
            if self.sector_size == 512:
                size &= 0xFFFFFFFF
            name = raw[:max(0, min(64, length) - 2)].decode("utf-16-le", "replace")
            entries.append(Entry(index, name, raw[66], left, right, child, start_sector, size))
        return entries

    def children(self, entry: Entry) -> dict:
        """A storage's entries by upper-case name (names compare without case)."""
        if entry._children is None:
            found, stack, seen = {}, [entry.child], set()
            while stack:
                index = stack.pop()
                if index == _NO_ENTRY or index >= len(self.entries) or index in seen:
                    continue
                seen.add(index)
                node = self.entries[index]
                if node.kind in (STORAGE, STREAM):
                    found.setdefault(node.name.upper(), node)
                stack.extend((node.left, node.right))
            entry._children = found
        return entry._children

    def find(self, path: str) -> Optional[Entry]:
        """The entry at a `/`-separated path from the root, or None."""
        node = self.root
        for part in (p for p in path.split("/") if p):
            if node.kind == STREAM:
                return None
            node = self.children(node).get(part.upper())
            if node is None:
                return None
        return node

    # -- streams ---------------------------------------------------------------------

    def _is_mini(self, entry: Entry) -> bool:
        return entry.kind != ROOT and entry.size < self.cutoff

    def _chain_to(self, entry: Entry, last: int) -> list:
        """The stream's chain, followed at least to its `last`-th unit."""
        chain = entry._chain
        if len(chain) > last or entry._chain_ended:
            return chain
        mini = self._is_mini(entry)
        limit = -(-entry.size // (self.mini_size if mini else self.sector_size))

        def following(unit):
            if mini:
                return self._minifat[unit] if unit < len(self._minifat) else _NO_ENTRY
            return self._next(unit)

        unit = following(chain[-1]) if chain else entry.start
        while len(chain) <= last:
            # A chain longer than its stream's size is a loop: stop there.
            if unit > _MAX_REGULAR or len(chain) >= limit:
                entry._chain_ended = True
                break
            chain.append(unit)
            unit = following(unit)
        return chain

    def read(self, entry: Entry, offset: int = 0, size: Optional[int] = None) -> bytes:
        """`size` bytes of a stream from `offset` (to its end when None); bytes
        a cut file or a broken chain does not hold read as zeros."""
        offset = max(0, int(offset))
        end = entry.size if size is None else min(entry.size, offset + max(0, int(size)))
        if end <= offset:
            return b""
        mini = self._is_mini(entry)
        unit = self.mini_size if mini else self.sector_size
        first, last = offset // unit, (end - 1) // unit
        units = self._chain_to(entry, last)[first:last + 1]
        parts = []
        at = first * unit                  # the stream offset of the run below
        for start, count in _runs(units):
            low, high = max(at, offset), min(at + count * unit, end)
            skip = low - at
            if mini:
                data = self.read(self.root, start * unit + skip, high - low)
            else:
                data = self._read_at(self._sector_offset(start) + skip, high - low)
            parts.append(data.ljust(high - low, b"\x00"))
            at += count * unit
        return b"".join(parts).ljust(end - offset, b"\x00")

    def close(self):
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()


def _runs(numbers):
    """`(first, count)` of each run of consecutive numbers, in order."""
    runs: list = []
    for number in numbers:
        if runs and number == runs[-1][0] + runs[-1][1]:
            runs[-1][1] += 1
        else:
            runs.append([number, 1])
    return [(first, count) for first, count in runs]

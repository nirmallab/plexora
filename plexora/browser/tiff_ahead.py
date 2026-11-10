"""A picked TIFF's directories read ahead of tifffile's walk, a round at a time.

tifffile opens a TIFF by walking it: the header names the first directory
(IFD), each IFD names the next, and a pyramid's SubIFDs list names its levels'
IFDs. Each step reads what the one before it named. On the website a read of a
picked file the worker does not hold is one synchronous read of the 1 MiB block
around it, where the file lies (workers/pythonWorker.js `blockReads`), after
the last. A 19-channel Bio-Formats OME-TIFF on the lab share opened in 8 such
reads -- the header, the block holding all 19 IFDs, the SubIFDs lists at the
file's end, a block for each of five levels the lists had named together --
and from Chrome a 1 MiB read of a block not read lately took ~220 ms there,
a 64 KiB one ~20 ms: 1.8 s for an open that needs well under 1 MiB.

`install` makes the same walk first whenever tifffile opens a picked file to
read, and asks the page for what each step names, every 64 KiB piece of it at
once (`read_ahead`): the walk takes as many rounds as the file's layout is
deep, each reading only what tifffile will read there, and tifffile's walk then
finds it all held. The first round reads the file's last piece beside its
header, which is where Bio-Formats writes the SubIFDs lists; a main chain whose
IFDs sit together is read ahead twice as far as it has come, so 19 IFDs are
not 19 rounds. What is walked is what tifffile reads as it opens a file -- the
chain of main IFDs, each one's SubIFDs, and the values tifffile loads with an
IFD (`TIFF.TAG_LOAD`, `TIFF.TAG_READERS`) -- within a budget (`BUDGET` bytes,
`ROUNDS` rounds), past which tifffile reads the rest as it always did. Where
Python cannot wait on the page's reads (a browser without JSPI) `read_ahead`
answers no, and nothing is walked. Nothing here changes what tifffile reads or
how: a walk that fails is only a walk not made.
"""

from __future__ import annotations

import os
import struct
from collections import deque

#: Bytes one walk reads at most.
BUDGET = 8 << 20
#: Rounds one walk takes at most: a chain spread wider is tifffile's to read.
ROUNDS = 8
#: Main IFDs one walk follows at most (a stack of thousands of planes).
MAX_IFDS = 4096
#: Tags in one IFD, or offsets in one SubIFDs list, past which the bytes are
#: taken for something other than a directory.
MAX_ENTRIES = 4096
#: Values the worker reads straight through rather than in blocks (`blockReads`).
VALUE_MAX = 4 << 20
#: What an IFD not yet read is asked with: its count and a couple of hundred
#: entries, so its entries do not take a round of their own.
IFD_GUESS = 4096
#: A main IFD this close after the last is on a chain read ahead
#: (`_Walk.chain`), at most `CHAIN_MAX` at once. A chain whose IFDs sit
#: further apart has its planes between them, read ahead for nothing.
DENSE = 64 << 10
CHAIN_MAX = 2 << 20
#: Pieces a file may be and be read whole in the first round: the size of one
#: of the worker's blocks, which is all tifffile alone would read of it.
WHOLE = 16
#: SubIFDs.
SUBIFDS = 330

#: The last walks, newest last: `{path, rounds, pieces, ifds}` each.
walks = deque(maxlen=8)

_tags = None


def _loaded():
    """`(tags whose values tifffile reads with an IFD, those read by a reader
    of their own, bytes per value of each data type)`, from tifffile itself,
    so the walk keeps step with its version."""
    global _tags
    if _tags is None:
        import tifffile

        sizes = {dtype: struct.calcsize("<" + fmt) for dtype, fmt in tifffile.TIFF.DATA_FORMATS.items()}
        _tags = (frozenset(tifffile.TIFF.TAG_LOAD), frozenset(tifffile.TIFF.TAG_READERS), sizes)
    return _tags


def walk(fh, size: int, fetch, piece: int) -> dict:
    """Walk the TIFF `fh` reads (`size` bytes) as tifffile will open it,
    reading only from pieces asked for: `fetch(pieces)` reads the pieces (of
    `piece` bytes, by index) a step names, at once, and returns how many it
    read -- negative for a file it does not hold, which ends the walk.
    -> `{rounds, pieces, ifds}`."""
    walked = _Walk(fh, size, fetch, piece)
    walked.run()
    return {"rounds": walked.rounds, "pieces": len(walked.held), "ifds": walked.ifds}


class _Walk:
    def __init__(self, fh, size, fetch, piece):
        self.fh, self.size, self.fetch, self.piece = fh, size, fetch, piece
        self.budget = max(1, BUDGET // piece)
        self.held = set()
        self.rounds = 0
        self.ifds = 0
        self.first = self.last = None

    def missing(self, offset, length):
        first, last = offset // self.piece, (offset + max(length, 1) - 1) // self.piece
        return {index for index in range(first, last + 1) if index not in self.held}

    def read(self, offset, length):
        self.fh.seek(offset)
        data = self.fh.read(length)
        if len(data) != length:
            raise EOFError(offset)
        return data

    def ask(self, wanted, later):
        """One round: `wanted` (what the walk needs to go on) first, then
        `later` (values tifffile loads), within the budget. False: stop."""
        room = self.budget - len(self.held)
        if self.rounds >= ROUNDS or room <= 0:
            return False
        asked = sorted(wanted - self.held)[:room]
        asked += sorted(later - self.held - set(asked))[:room - len(asked)]
        if not asked or self.fetch(asked) < 0:
            return False
        self.held.update(asked)
        self.rounds += 1
        return True

    def chain(self, at):
        """Past a main IFD at `at` not yet read: twice as far as the main
        chain has come, when its IFDs sit together (each within `DENSE` of the
        last) -- a Bio-Formats file's 19 IFDs, 19 KB apart, in 3 rounds."""
        if self.last is None or not 0 < at - self.last <= DENSE:
            return set()
        return self.missing(at, min(2 * (at - self.first), CHAIN_MAX, self.size - at))

    def run(self):
        last = (self.size - 1) // self.piece
        first = set(range(last + 1)) if last < WHOLE else {0, last}
        if self.size < 16 or not self.ask(first, set()):
            return
        head = self.read(0, 16)
        order = {b"II": "<", b"MM": ">"}.get(head[:2])
        if order is None:
            return
        version = struct.unpack(order + "H", head[2:4])[0]
        if version == 42:
            self.count, self.entry, self.offset, first = "H", "HHI4s", "I", head[4:8]
        elif version == 43:
            self.count, self.entry, self.offset, first = "Q", "HHQ8s", "Q", head[8:16]
        else:
            return
        self.order = order
        self.countsize = struct.calcsize(order + self.count)
        self.entrysize = struct.calcsize(order + self.entry)
        self.offsetsize = struct.calcsize(order + self.offset)
        self.first = struct.unpack(order + self.offset, first)[0]
        # IFDs to parse, as (offset, on the main chain); SubIFDs lists stored
        # out of line, as (offset, count, format); pieces of values.
        pending = [(self.first, True)]
        lists = []
        values = set()
        seen = set()
        while True:
            wanted = set()
            moved = True
            while moved:
                moved = False
                waiting = []
                for at, main in pending:
                    if at in seen or not 8 <= at < self.size:
                        continue
                    if self.missing(at, self.countsize):
                        need = self.missing(at, min(IFD_GUESS, self.size - at))
                        if main:
                            need |= self.chain(at)
                    else:
                        count = struct.unpack(order + self.count, self.read(at, self.countsize))[0]
                        span = self.countsize + count * self.entrysize + self.offsetsize
                        if not 0 < count <= MAX_ENTRIES or at + span > self.size:
                            seen.add(at)
                            continue
                        need = self.missing(at, span)
                    if need:
                        wanted |= need
                        waiting.append((at, main))
                        continue
                    seen.add(at)
                    moved = True
                    waiting += self.parse(at, count, main, lists, values)
                pending = waiting
                unread = []
                for at, count, fmt in lists:
                    need = self.missing(at, count * struct.calcsize(fmt))
                    if need:
                        wanted |= need
                        unread.append((at, count, fmt))
                        continue
                    moved = True
                    data = self.read(at, count * struct.calcsize(fmt))
                    pending += [(offset, False) for offset in struct.unpack(f"{order}{count}{fmt}", data)]
                lists = unread
            later = values - self.held
            if not wanted and not later:
                return
            if not self.ask(wanted, later):
                return

    def parse(self, at, count, main, lists, values):
        """The IFD at `at`, from held pieces: its SubIFDs and next IFD to
        walk, its SubIFDs lists stored out of line into `lists`, and the
        pieces of the values tifffile loads with it into `values`."""
        load, readers, sizes = _loaded()
        order, inline = self.order, self.offsetsize
        raw = self.read(at + self.countsize, count * self.entrysize + self.offsetsize)
        found = []
        for i in range(count):
            tag, dtype, number, field = struct.unpack_from(order + self.entry, raw, i * self.entrysize)
            itemsize = sizes.get(dtype)
            if itemsize is None:
                continue
            nbytes = number * itemsize
            if tag == SUBIFDS and itemsize in (4, 8) and 0 < number <= MAX_ENTRIES:
                fmt = "I" if itemsize == 4 else "Q"
                if nbytes <= inline:
                    found += [(offset, False) for offset in struct.unpack_from(f"{order}{number}{fmt}", field)]
                else:
                    lists.append((struct.unpack_from(order + self.offset, field)[0], number, fmt))
            elif tag in readers or (nbytes > inline and tag in load):
                where = struct.unpack_from(order + self.offset, field)[0]
                if 8 <= where and nbytes <= VALUE_MAX and where + nbytes <= self.size:
                    values |= self.missing(where, nbytes)
        if main:
            self.ifds += 1
            self.last = at
            following = struct.unpack_from(order + self.offset, raw, count * self.entrysize)[0]
            if following and self.ifds < MAX_IFDS:
                found.append((following, True))
        return found


def ahead(path: str, read_ahead, piece: int) -> dict | None:
    """`walk` the file at `path`, `read_ahead(path, pieces)` reading its
    pieces: what the walk did, or None when it made none."""
    try:
        size = os.path.getsize(path)
        # Unbuffered: what is read is exactly what the walk asked for.
        with open(path, "rb", buffering=0) as fh:
            done = walk(fh, size, lambda pieces: read_ahead(path, pieces), piece)
    except Exception:  # noqa: BLE001 -- the open's errors are tifffile's to raise
        return None
    if done["rounds"]:
        walks.append({"path": path, **done})
    return done


def install(read_ahead, piece: int) -> bool:
    """tifffile's `FileHandle.open` walks a file it opens to read (`ahead`)
    before tifffile reads anything of it. `read_ahead(path, pieces)` reads
    pieces of `piece` bytes at once and holds them for the reads that follow:
    how many it read, negative for a file that is not one the page holds.
    True if installed."""
    import tifffile

    original = tifffile.FileHandle.open
    if getattr(original, "plexora_ahead", False):
        return True

    def opened(self):
        fresh = self._fh is None
        original(self)
        if fresh and isinstance(self._file, str) and self._mode == "rb" and not self._offset:
            ahead(self._file, read_ahead, piece)

    opened.__doc__ = original.__doc__
    opened.plexora_ahead = True
    tifffile.FileHandle.open = opened
    return True

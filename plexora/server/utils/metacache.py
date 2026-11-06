"""Header reads remembered per file, while the file has not changed.

A gating session asks the same image's TIFF header for its pixel size and its
layout hundreds of times (QC's input fingerprints call `pixel_scale.pixel_size`
for every cell-exclusion check; `brightfield.is_rgb_layout` reads the header
again inside it). Each read opens the file and parses its first IFD: about 6 s
of a 17.5 s session on CPython, measured in the browser proof
(docs/internal/bench/browser_node/P9.md, the metacache record).

The key is (path, size, mtime_ns), so a file rewritten in place is read again.
Only regular files are kept: a directory's mtime does not move when a file
inside it is rewritten (an OME-Zarr's attributes), so a directory is read every
time, as before.
"""

from __future__ import annotations

import functools
import os
import stat
import threading
from collections import OrderedDict

#: Files remembered per reader.
MAX_FILES = 64


def per_file(read):
    """`read(path)`, remembered per (path, size, mtime_ns) for regular files.

    A dict answer is handed out as a copy, so a caller that adds to it cannot
    change what the next caller is told.
    """
    kept: OrderedDict = OrderedDict()
    lock = threading.Lock()

    @functools.wraps(read)
    def remembered(path):
        try:
            facts = os.stat(path)
        except (OSError, TypeError, ValueError):
            return read(path)
        if not stat.S_ISREG(facts.st_mode):
            return read(path)
        key = (os.fspath(path), facts.st_size, facts.st_mtime_ns)
        with lock:
            if key in kept:
                kept.move_to_end(key)
                answer = kept[key]
                return dict(answer) if isinstance(answer, dict) else answer
        answer = read(path)
        with lock:
            kept[key] = answer
            while len(kept) > MAX_FILES:
                kept.popitem(last=False)
        return dict(answer) if isinstance(answer, dict) else answer

    def forget():
        with lock:
            kept.clear()

    remembered.forget = forget
    return remembered

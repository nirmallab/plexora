"""A large flat image from a smaller one (the envelope's flat-build cell).

Envelope.json's build rate (29 MB/s a band worker) was measured on P7's
flat image: LSP11385's two channels written flat as zlib strips of two rows
(tools/poc_browser_node/bench/fixtures.json `M.flatImages.strips`, 9.6 GB of
pixels). This repeats that file's channels `times` times into one OME
BigTIFF, copying the compressed strips as they are (no re-encoding), so the
pixels, the codec and the strip layout are the ones the rate was measured on:
4 times is 8 channels, 38.4 GB of pixels (about 12 GB on disk).

    python tools/bench_envelope/flat_big.py SRC OUT --times 4
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import tifffile


def make(source: Path, out: Path, times: int) -> dict:
    with tifffile.TiffFile(source) as tif:
        pages = list(tif.pages)
        first = pages[0]
        height, width = first.shape
        rows = first.rowsperstrip
        if first.is_tiled or any(page.shape != first.shape for page in pages):
            raise SystemExit(f"{source}: expected flat stripped planes of one shape")
        handle = tif.filehandle

        def strips():
            for _ in range(times):
                for page in pages:
                    for offset, count in zip(page.dataoffsets, page.databytecounts):
                        handle.seek(offset)
                        yield handle.read(count)

        channels = len(pages) * times
        with tifffile.TiffWriter(out, bigtiff=True, ome=True) as writer:
            writer.write(strips(), shape=(channels, height, width), dtype=first.dtype,
                         compression=first.compression, rowsperstrip=rows, photometric="minisblack",
                         metadata={"axes": "CYX", "Channel": {"Name": [f"C{i}" for i in range(channels)]}})
    return {"shape": [channels, height, width], "dtype": str(first.dtype),
            "raw_bytes": channels * height * width * first.dtype.itemsize, "file_bytes": out.stat().st_size}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--times", type=int, default=4)
    args = parser.parse_args(argv)
    print(json.dumps(make(args.source, args.out, args.times)))


if __name__ == "__main__":
    main()

"""Reading a vendor's transcript table.

One adapter per format, dispatched on what the file looks like -- NOT a direct
coupling to Xenium. The rest of Plexora knows only `read_transcripts`, which
hands back the four arrays `transcript_tiles.build` takes, so a Merscope or a
CosMx reader is a function added here and nothing else changed anywhere.

**Positions arrive in MICRONS and leave in REFERENCE PIXELS.** Xenium's
`x_location` is a physical coordinate, not a pixel index, and treating one as the
other is wrong by the pixel size -- which for a 0.2125 um/px Xenium run is a
factor of nearly five. The conversion happens once, here, because this is the
only place that knows both the file's units and the image's calibration; every
layer downstream is in reference pixels and says so.

pyarrow rather than polars, and for a specific reason: a 50-million-row
transcripts.parquet is 1-6 GB, and pyarrow can read ONE COLUMN at a time out of
it. Reading three columns rather than the whole table keeps the peak at what is
actually needed, which is the difference between working on a laptop and not.

pyarrow is present in every Plexora build already (pandas imports it), so this is
not an added dependency -- but it is not DECLARED anywhere, which is why the
import is still guarded and reports something a user can act on.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from plexora.server.utils import spatial_scene as _core_scene

#: What Xenium calls things. Named as constants because a typo here reads as
#: "this file has no transcripts" rather than as a typo.
XENIUM_GENE = "feature_name"
XENIUM_X = "x_location"
XENIUM_Y = "y_location"
XENIUM_QV = "qv"

#: Xenium's own recommended quality threshold. Applied only when asked for:
#: the default is everything the file holds, because silently dropping a third
#: of somebody's data is not a viewer's decision to make.
DEFAULT_QV = None

#: Control probes and other non-gene features. Xenium writes these into the same
#: column as real genes, and a panel listing "NegControlProbe_00042" beside EPCAM
#: is a gene selector nobody can use.
CONTROL_PREFIXES = ("NegControlProbe", "NegControlCodeword", "antisense",
                    "BLANK", "UnassignedCodeword", "DeprecatedCodeword",
                    "Intergenic")


class UnsupportedTranscriptFile(Exception):
    """This file is not one any adapter here can read."""


class TranscriptDependencyMissing(Exception):
    """pyarrow is not importable.

    Its own exception rather than a bare ImportError, so the requirements modal
    can offer the install line the way `BrightfieldSupportMissing` already does
    for `.mrxs`. pandas brings pyarrow in every environment this has been
    measured in, so this should not fire -- but it is transitive rather than
    declared, and "ModuleNotFoundError: pyarrow" in a server log is not
    something to hand a biologist.
    """

    INSTALL = 'pip install "plexora[spatial]"'


#: The column check, and the footer peek, as core states them.
#:
#: Re-exported rather than implemented twice. The IMPORTER has to be able to
#: say "this parquet is transcripts" while proposing an import, and a core build
#: may not import a plugin (tests/test_plugin_boundary.py) -- so the three
#: column names live in `spatial_scene` and this is the same function under the
#: name this plugin's own reader has always used. What is NOT in core is
#: everything below: the quality filter, the control-probe list, the
#: micron-to-pixel conversion and the vocabulary, which is the interpretation
#: and is why this plugin exists.
is_xenium_transcripts = _core_scene.is_xenium_transcripts


def peek(path):
    """What this file holds, without reading a row of it.

    Core's footer read, under this plugin's own name for the answer. Xenium is
    what THIS reader knows how to read, and core -- which has to be able to say
    "that parquet is transcripts" while proposing an import -- deliberately
    does not name a vendor. So the fact is core's and the wording is the
    plugin's, which is the whole boundary in one function.
    """
    found = _core_scene.peek_parquet(path)
    if found is None:
        return None
    return {**found, "is_xenium": found["is_transcripts"]}


def read_transcripts(path, *, pixel_size=None, min_qv=DEFAULT_QV,
                     drop_controls=True, progress=None):
    """A transcript file as (genes, gene_index, x, y).

    @param pixel_size - microns per pixel of the REFERENCE image. None leaves
        the coordinates as the file states them, which is right only when the
        file is already in pixels -- so callers that have a calibration should
        always pass it.
    @param min_qv - drop transcripts below this quality score. None keeps
        everything, because throwing away a third of somebody's data is not a
        default a viewer gets to choose.
    @param drop_controls - drop Xenium's negative-control probes. They are real
        rows and they are not genes; a selector listing them beside EPCAM is one
        nobody can use.

    @returns (list[str] vocabulary, uint16 per point, float32 x, float32 y)
    """
    path = Path(path)
    if not is_xenium_transcripts(path):
        raise UnsupportedTranscriptFile(
            f"{path.name} does not carry Xenium's transcript columns")
    try:
        import pyarrow.parquet as pq
    except ImportError as error:
        raise TranscriptDependencyMissing(
            "Reading transcripts needs pyarrow. "
            f"Install it with: {TranscriptDependencyMissing.INSTALL}") from error

    handle = pq.ParquetFile(str(path))
    columns = [XENIUM_GENE, XENIUM_X, XENIUM_Y]
    has_qv = XENIUM_QV in set(handle.schema_arrow.names)
    if min_qv is not None and has_qv:
        columns.append(XENIUM_QV)

    # One column at a time: a 50-million-row file is 1-6 GB, and reading the
    # whole table to take three columns of it is the difference between working
    # on a laptop and not.
    table = handle.read(columns=columns)
    if progress:
        progress("read", 1, 1)

    names = table.column(XENIUM_GENE).to_numpy(zero_copy_only=False)
    x = np.asarray(table.column(XENIUM_X).to_numpy(zero_copy_only=False), dtype=np.float64)
    y = np.asarray(table.column(XENIUM_Y).to_numpy(zero_copy_only=False), dtype=np.float64)
    del table

    keep = np.isfinite(x) & np.isfinite(y)
    if min_qv is not None and has_qv:
        # Re-read rather than held: the quality column is another 200 MB at 50
        # million rows and it is needed for exactly one comparison.
        qv = np.asarray(
            handle.read(columns=[XENIUM_QV]).column(XENIUM_QV).to_numpy(zero_copy_only=False),
            dtype=np.float32)
        keep &= qv >= float(min_qv)
        del qv

    names = np.asarray(names, dtype=object)
    if drop_controls:
        keep &= ~np.fromiter(
            (is_control(str(n)) for n in names), dtype=bool, count=len(names))

    if not keep.all():
        names, x, y = names[keep], x[keep], y[keep]

    vocabulary, gene_index = np.unique(names, return_inverse=True)
    if progress:
        progress("index", 1, 1)

    scale = 1.0 / float(pixel_size) if pixel_size else 1.0
    return (
        [str(v) for v in vocabulary],
        gene_index.astype(np.uint16, copy=False),
        (x * scale).astype(np.float32, copy=False),
        (y * scale).astype(np.float32, copy=False),
    )


def is_control(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in CONTROL_PREFIXES)

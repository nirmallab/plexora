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


#: The panel file a Xenium run ships beside its outputs, and how to walk it.
#: The vocabulary read from here is the PANEL -- every gene the run was
#: designed to detect -- which is not the same list as the genes that happen
#: to appear in the transcript table. A gene with zero calls in this section
#: still belongs in the selector, greyed at zero, because "we looked and found
#: none" is a result and an absent row is not.
GENE_PANEL_FILE = "gene_panel.json"


def read_gene_panel(path):
    """The gene names a run's panel declares, in the panel's own order, or None.

    `path` may be the panel file or the run directory that holds it. Only
    targets whose descriptor is `gene` -- the negative controls are in the
    same list and are not genes.
    """
    import json

    path = Path(path)
    if path.is_dir():
        path = path / GENE_PANEL_FILE
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    targets = (((doc.get("payload") or {}).get("targets")) or [])
    found = []
    for target in targets:
        kind = target.get("type") or {}
        if kind.get("descriptor") != "gene":
            continue
        name = ((kind.get("data") or {}).get("name") or "").strip()
        if name:
            found.append(str(name))
    return found or None


def read_transcripts(path, *, pixel_size=None, transform=None,
                     min_qv=DEFAULT_QV, drop_controls=True, vocabulary=None,
                     progress=None):
    """A transcript file as (genes, gene_index, x, y, q).

    @param pixel_size - microns per pixel of the REFERENCE image. None leaves
        the coordinates as the file states them, which is right only when the
        file is already in pixels -- so callers that have a calibration should
        always pass it.
    @param transform - the LAYER's own affine into reference pixels,
        `[a, b, c, d, e, f]`, which outranks `pixel_size` when both are given.
        It is the thing that is actually true: the importer composed it when
        the run was registered, from the run's own manifest, and it survives a
        project whose `ImageSpec` never recorded a pixel size -- which is the
        state every Xenium import was in, and is why a 19-million-point cache
        was built five times too small and drawn in the top-left corner of the
        slide.
    @param min_qv - drop transcripts below this quality score at BUILD time.
        None keeps everything, which is the default and the right one: the
        score travels with each point, the client filters in its shader, and a
        threshold baked in here could only be undone by rebuilding.
    @param drop_controls - drop Xenium's negative-control probes. They are real
        rows and they are not genes; a selector listing them beside EPCAM is one
        nobody can use.
    @param vocabulary - the gene list to index against, from the run's panel.
        Without it the vocabulary is whatever names appear in the table, which
        silently omits every gene with no calls in this section.

    @returns (list[str] vocabulary, uint16 per point, float32 x, float32 y,
              uint8 q)
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
    has_qv = XENIUM_QV in set(handle.schema_arrow.names)
    columns = [XENIUM_GENE, XENIUM_X, XENIUM_Y]
    if has_qv:
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
    qv = (np.asarray(table.column(XENIUM_QV).to_numpy(zero_copy_only=False),
                     dtype=np.float32) if has_qv
          else np.full(len(x), 255.0, dtype=np.float32))
    del table

    keep = np.isfinite(x) & np.isfinite(y)
    if min_qv is not None and has_qv:
        keep &= qv >= float(min_qv)

    names = np.asarray(names, dtype=object)
    if drop_controls:
        keep &= ~np.fromiter(
            (is_control(str(n)) for n in names), dtype=bool, count=len(names))

    if vocabulary:
        # Anything the panel does not name is dropped, which covers the
        # controls again and also the unassigned codewords a run emits under
        # names no panel declares.
        lookup = {str(name): index for index, name in enumerate(vocabulary)}
        indices = np.fromiter((lookup.get(str(n), -1) for n in names),
                              dtype=np.int64, count=len(names))
        keep &= indices >= 0
    else:
        indices = None

    if not keep.all():
        names, x, y, qv = names[keep], x[keep], y[keep], qv[keep]
        if indices is not None:
            indices = indices[keep]

    if indices is not None:
        vocabulary, gene_index = list(vocabulary), indices
    else:
        vocabulary, gene_index = np.unique(names, return_inverse=True)
        vocabulary = [str(v) for v in vocabulary]
    if progress:
        progress("index", 1, 1)

    px, py = _to_reference_pixels(x, y, transform=transform,
                                  pixel_size=pixel_size)
    return (
        vocabulary,
        np.asarray(gene_index).astype(np.uint16, copy=False),
        px.astype(np.float32, copy=False),
        py.astype(np.float32, copy=False),
        np.clip(np.rint(qv), 0, 255).astype(np.uint8, copy=False),
    )


def _to_reference_pixels(x, y, *, transform=None, pixel_size=None):
    """Micron coordinates in the reference image's pixel grid.

    The layer's own affine first. It is the registration the importer composed
    when the run was registered and the one every OTHER layer in the sample is
    drawn through, so using anything else here would mean the transcripts
    agreed with the image and disagreed with the cells.
    """
    if transform and len(transform) == 6:
        a, b, c, d, e, f = (float(v) for v in transform)
        return (a * x + c * y + e, b * x + d * y + f)
    scale = 1.0 / float(pixel_size) if pixel_size else 1.0
    return (x * scale, y * scale)


def is_control(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in CONTROL_PREFIXES)

"""Hand-written spatial transcriptomics inputs for the tests.

The same rule `ngff_fixtures.py` follows, for the same reason: the point of most
of these is a shape a real writer rarely emits -- a translation with no scale, a
rotation written as an affine, a shear nobody supports -- and writing the
metadata by hand is what keeps the reader's tests honest about the spec rather
than about one library's interpretation of it.

No real Xenium run is available here, and a 10-100 million transcript file is not
something to check in anyway. What these produce is the SHAPE: Xenium's own
column names, a coordinate system per element, an element that lands in a system
the reference does not.

Budget: nothing here writes more than 512x512x3 levels or 50k transcripts. A
fixture costing a second in a suite of several thousand tests is a fixture that
gets deleted.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

#: Xenium's own column names, exactly. Getting one wrong here would make the
#: reader's tests pass against a file Xenium never writes -- which is the one
#: failure a fixture must not have.
XENIUM_COLUMNS = ("cell_id", "transcript_id", "feature_name",
                  "x_location", "y_location", "z_location", "qv")


def write_transcripts_parquet(path, *, genes=("EPCAM", "CD3E", "PTPRC"),
                              n=5_000, width=512, height=512, seed=0):
    """A transcripts.parquet with Xenium's columns and Xenium's spelling.

    Positions are in MICRONS in a real run -- `x_location` is not a pixel index
    -- so a reader that treats them as pixels is wrong by the pixel size. The
    fixture keeps them in the same numeric range as the image on purpose: that
    makes the pixel-size mistake invisible here, which is why
    test_transcript_tiles asserts the conversion separately rather than relying
    on the coordinates looking right.

    Returns the path, and writes nothing if pyarrow is unavailable -- see
    `has_pyarrow`.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    rng = np.random.default_rng(seed)
    feature = rng.choice(np.array(genes, dtype=object), size=n)
    table = pa.table({
        "cell_id": pa.array(rng.integers(1, max(2, n // 10), size=n).astype("int64")),
        "transcript_id": pa.array(np.arange(n, dtype="int64")),
        "feature_name": pa.array(feature.tolist(), type=pa.string()),
        "x_location": pa.array(rng.uniform(0, width, size=n).astype("float32")),
        "y_location": pa.array(rng.uniform(0, height, size=n).astype("float32")),
        "z_location": pa.array(rng.uniform(0, 20, size=n).astype("float32")),
        # Xenium's quality score. 20 is the vendor's own "keep" threshold, so a
        # fixture with values either side of it is what makes a filter testable.
        "qv": pa.array(rng.uniform(5, 40, size=n).astype("float32")),
    })
    path = Path(path)
    pq.write_table(table, str(path))
    return path


def has_pyarrow():
    """Whether the transcript reader's one dependency is importable.

    pyarrow arrives transitively with spatialdata rather than being declared, so
    a build without the spatial extra genuinely may not have it -- and a test
    that fails for that reason is reporting the wrong thing.
    """
    try:
        import pyarrow  # noqa: F401
    except ImportError:
        return False
    return True


# -- coordinate transformations --------------------------------------------

def scale(*values, output="global"):
    return {"type": "scale", "scale": list(values), "output": {"name": output}}


def translation(*values, output="global"):
    return {"type": "translation", "translation": list(values), "output": {"name": output}}


def rotation(degrees, output="global"):
    """A rotation as NGFF writes one: an affine, in axis order (y, x).

    Rotations do not have a transform type of their own in NGFF, so every writer
    emits them this way -- which is why the reader has to handle `affine` at all.
    """
    r = math.radians(degrees)
    return {
        "type": "affine",
        "affine": [[math.cos(r), -math.sin(r), 0.0],
                   [math.sin(r), math.cos(r), 0.0]],
        "output": {"name": output},
    }


def shear(amount=0.5, output="global"):
    """A transform OpenSeadragon cannot express, so the refusal can be pinned.

    No real registration produces one, which is the point: the viewer has to
    REFUSE it rather than approximate it, because a sheared layer drawn without
    its shear looks entirely plausible and is wrong everywhere.
    """
    return {
        "type": "affine",
        "affine": [[1.0, 0.0, 0.0], [amount, 1.0, 0.0]],
        "output": {"name": output},
    }


def anisotropic(sx=1.0, sy=2.0, output="global"):
    return scale(sy, sx, output=output)


def write_element_attrs(path, transforms, *, axes=("y", "x"), zarr_format=2):
    """One element's coordinate systems, written as plain JSON.

    Deliberately not through zarr: what the reader parses IS this file, and a
    fixture that goes through a writer tests the writer's idea of the spec
    rather than the file a user will actually hand us.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    payload = {
        "axes": [{"name": name} for name in axes],
        "coordinateTransformations": list(transforms),
    }
    if zarr_format == 2:
        (path / ".zattrs").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    else:
        (path / "zarr.json").write_text(
            json.dumps({"zarr_format": 3, "node_type": "group", "attributes": payload},
                       indent=1),
            encoding="utf-8")
    return path


def write_spatialdata_store(path, *, elements=None, zarr_format=2):
    """A store whose elements each declare where they land.

    `elements` maps `"<kind>/<name>"` to that element's transformation list --
    so a caller says exactly which element is registered how, including the case
    that matters most: one that reaches a coordinate system the reference does
    not, which must read back as "not registered" rather than as the identity.

    Only the metadata is written. Nothing here reads a pixel, and a fixture that
    wrote arrays would cost a second per test for data no assertion looks at.
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    elements = elements or {
        "images/morphology": [scale(0.2125, 0.2125)],
        "points/transcripts": [],
    }
    for key, transforms in elements.items():
        write_element_attrs(path / key, transforms, zarr_format=zarr_format)
    return path

"""A cell table dropped together with its own slide belongs to that slide.

With a sample open, dropping a new slide and the table made from it is ONE new
sample: the table goes with its image to the importer. It is never attached to
the sample that happened to be open, which would give that tissue another
slide's cells -- gates, phenotypes and spatial results from the wrong tissue.
"""

import numpy as np
import tifffile

from tests.test_drop_plan import _path, _plan, _register, _source, client  # noqa: F401


def test_a_table_dropped_with_its_own_image_is_not_attached_to_the_open_sample(client):  # noqa: F811
    tifffile.imwrite(_source() / "tonsil.ome.tif",
                     np.ones((2, 64, 64), dtype=np.uint16))
    open_sample = _register(client, "tonsil.ome.tif")

    plan = _plan(client, "slide.ome.tif", "slide.csv", datasource=open_sample)

    onto_open = [a for a in plan["actions"]
                 if a["kind"] == "attach_table"
                 or ((a.get("route") or {}).get("json") or {}).get("sample")
                 == open_sample]
    assert onto_open == [], (
        "the new slide's table was attached to the open sample: "
        f"{[(a['kind'], a['status'], a['sample'], a['paths']) for a in onto_open]}")

    # The new slide is imported WITH its table (one press), or the drop asks.
    importing = [a for a in plan["actions"] if a["kind"] == "import_new"]
    if importing:
        assert importing[0]["route"]["json"]["paths"] == [
            _path("slide.ome.tif"), _path("slide.csv")]
    else:
        assert plan["summary"]["one_press"] is False

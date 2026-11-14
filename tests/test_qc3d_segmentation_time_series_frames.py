"""The 3-D segmentation check on a time series' object table.

`measure_objects` on a time series writes one row per object per frame, its
frame in `t` (server/volume/objects.py). With a label volume per frame the
frames' objects are unrelated: a cell that ends on one plane in frame 0 and a
different cell that starts on the next plane at the same place in frame 1 are
not one cell split between planes.
"""


def _table(rows):
    import polars as pl

    frame = pl.DataFrame(rows, schema=["t", "label", "bbox_z0", "bbox_z1", "bbox_x0", "bbox_x1",
                                       "bbox_y0", "bbox_y1", "volume_vox", "touches_z_bounds"],
                         orient="row")
    return frame.with_columns(
        (pl.col("bbox_z1") - pl.col("bbox_z0") + 1).alias("n_planes"),
        ((pl.col("bbox_x0") + pl.col("bbox_x1")) / 2).alias("X"),
        ((pl.col("bbox_y0") + pl.col("bbox_y1")) / 2).alias("Y"),
        ((pl.col("bbox_z0") + pl.col("bbox_z1")) / 2 + 0.5).alias("Z"))


def test_qc3d_does_not_call_cells_of_different_frames_one_split_cell():
    from plexora.server.volume import qc3d

    rows = [
        # t, label, z0, z1, x0, x1, y0, y1, volume_vox, touches
        (0, 1, 2, 4, 60, 69, 40, 49, 280, False),    # frame 0: a cell ending on plane 4
        (0, 2, 3, 6, 10, 19, 10, 19, 300, False),
        (0, 3, 3, 6, 30, 39, 10, 19, 310, False),
        (0, 4, 2, 4, 90, 99, 40, 49, 285, False),    # frame 0: a cell cut between planes 4
        (0, 5, 5, 7, 90, 99, 40, 49, 275, False),    # and 5, and its other half
        (1, 7, 5, 7, 60, 69, 40, 49, 270, False),    # frame 1: another cell, starting on 5
        (1, 8, 3, 6, 10, 19, 70, 79, 305, False),
        (1, 9, 3, 6, 30, 39, 70, 79, 295, False),
    ]
    findings, flagged = qc3d.segmentation_3d(_table(rows), depth=14)
    # A split inside one frame is still found...
    assert "split_z" in flagged.get(4, []) and "split_z" in flagged.get(5, []), flagged
    # ...but frame 0's cell 1 and frame 1's cell 7 are not one cell.
    assert "split_z" not in flagged.get(1, []), flagged
    assert "split_z" not in flagged.get(7, []), flagged
    split = [f for f in findings if f["kind"] == "split_z"]
    assert split and split[0]["count"] == 2, findings

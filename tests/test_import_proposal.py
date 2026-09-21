"""What Plexora makes of the files somebody pointed at.

The engine under Import Sample, tested where it is cheapest to test: over paths,
with nothing registered and no data root involved. That split is deliberate --
`import_proposal` answers "what would this be" and `import_sample` writes it
down -- and it is what makes the interesting cases (a run with a folder for its
focus stack, two slides that might be two samples or one, a parquet nobody can
read without pyarrow) a handful of files in tmp_path rather than a fixture
project each.

Two rules are asserted throughout and are the whole design:

**Nothing is refused.** An unreadable file among five is reported and the other
four still import. A question nobody answered takes its default and is recorded.

**Nothing is opened.** The detection ladder reads headers, footers and directory
listings. The one exception is the mask tie-break, which reads one bounded
window of one file -- and only when the dtype and the plane count leave the
question genuinely open.
"""

import json

import numpy as np
import pytest
import tifffile

from plexora.server.models import import_proposal
from plexora.server.models.import_proposal import inspect_paths

from tests.spatial_fixtures import write_transcripts_parquet


def _image(path, shape=(3, 256, 256), dtype=np.uint16):
    tifffile.imwrite(path, np.random.default_rng(0).integers(
        0, 3000, shape).astype(dtype))
    return path


def _xenium_run(root, *, focus_folder=True, pixel_size=0.2125):
    """A Xenium run with the layout XOA 2.0 writes."""
    root.mkdir(parents=True, exist_ok=True)
    if focus_folder:
        (root / "morphology_focus").mkdir()
        _image(root / "morphology_focus" / "morphology_focus_0000.ome.tif",
               (2, 256, 256))
    else:
        _image(root / "morphology.ome.tif", (2, 256, 256))
    write_transcripts_parquet(root / "transcripts.parquet", n=2000,
                              width=1000, height=800)
    (root / "experiment.xenium").write_text(
        json.dumps({"pixel_size": pixel_size, "run_name": "demo"}),
        encoding="utf-8")
    return root


def _only(proposal):
    assert len(proposal.samples) == 1, [s.name for s in proposal.samples]
    return proposal.samples[0]


def _by_id(sample):
    return {layer.id: layer for layer in sample.layers}


# -- bundles ---------------------------------------------------------------

def test_a_xenium_run_is_one_sample_with_no_questions(tmp_path):
    """The default flow, and the reason "select a folder" is the primary action.

    One pick, one sample, the morphology image as the reference and everything
    else registered against it by the run's own pixel size. Nothing is asked,
    because nothing is ambiguous.
    """
    run = _xenium_run(tmp_path / "run_0042")
    sample = _only(inspect_paths([str(run)]))

    assert sample.name == "run_0042"
    assert sample.questions == []
    layers = _by_id(sample)
    assert layers["morphology"].reference is True
    assert layers["morphology"].modality == "xenium_morphology"
    assert layers["transcripts"].kind == "points"
    # Microns to reference pixels, which for a 0.2125 um/px run is a factor of
    # nearly five. Treating the file's coordinates as pixels is wrong by
    # exactly this and looks entirely plausible.
    assert layers["transcripts"].transform[0] == pytest.approx(1 / 0.2125)
    assert layers["transcripts"].transform_source == "run"
    assert sample.bundles[0]["format"] == "xenium"


def test_the_focus_stack_may_be_a_folder(tmp_path):
    """C9. XOA 2.0 writes `morphology_focus/` as a multi-file OME series, and
    the old table was filename-keyed and files only -- so a current run
    proposed no image at all."""
    run = _xenium_run(tmp_path / "run", focus_folder=True)
    sample = _only(inspect_paths([str(run)]))
    morphology = _by_id(sample)["morphology"]
    # The first file of the series, which is what tifffile follows from.
    assert morphology.src.endswith("morphology_focus_0000.ome.tif")
    assert morphology.geometry["width"] == 256


def test_the_morphology_row_says_which_of_the_runs_pictures_it_chose(tmp_path):
    """A run ships up to three pictures of one section and Plexora opens one.

    Which one is a decision with consequences -- the focus composite is what
    Xenium Explorer draws, the Z-stack is fourteen depths none of which covers
    the whole section -- and a row that does not say leaves the user no way to
    tell a correct import from an import of the wrong image.
    """
    run = _xenium_run(tmp_path / "run", focus_folder=True)
    morphology = _by_id(_only(inspect_paths([str(run)])))["morphology"]
    assert "focus composite" in morphology.detail


def test_a_z_stack_row_says_how_many_depths_it_set_aside(tmp_path):
    """The one fact that is in the file rather than in its name.

    A run with no focus image falls back to `morphology.ome.tif`, which is one
    stain at fourteen focal depths. It is read as ONE channel -- the middle
    depth -- and the row says so, because "1 channel" over a file that is
    plainly fourteen planes otherwise reads as a failure to open it.
    """
    run = tmp_path / "stack_run"
    run.mkdir()
    tifffile.imwrite(run / "morphology.ome.tif",
                     np.zeros((5, 64, 64), dtype=np.uint16),
                     ome=True, metadata={"axes": "ZYX"})
    write_transcripts_parquet(run / "transcripts.parquet", n=500)
    (run / "experiment.xenium").write_text(
        json.dumps({"pixel_size": 0.2125}), encoding="utf-8")

    morphology = _by_id(_only(inspect_paths([str(run)])))["morphology"]
    assert morphology.src.endswith("morphology.ome.tif")
    assert morphology.geometry["numChannels"] == 1
    assert "middle of 5 focal planes" in morphology.detail


def test_a_folder_that_only_wraps_a_run_proposes_the_run(tmp_path):
    """The wrapper directory a download unpacks into.

    A Xenium zip extracts to `<name>_outs/` inside a folder of the same name,
    and a run is often kept as `<sample>/outs/`. The folder the user thinks of
    as the sample held no loose files, so the answer used to be "nothing
    here" -- which reads as "Plexora cannot open Xenium data" and is one
    correct click away from being wrong.
    """
    run = _xenium_run(tmp_path / "download" / "run_0042_outs")
    sample = _only(inspect_paths([str(run.parent)]))

    assert sample.name == "run_0042_outs"
    assert sample.bundles[0]["format"] == "xenium"
    assert _by_id(sample)["morphology"].reference is True


def test_a_folder_holding_two_runs_is_left_alone(tmp_path):
    """Exactly one, and this is why.

    Picked paths that carry bundles are assembled into ONE sample, so
    descending into a folder of two runs would silently merge two slides --
    with one cell table, one set of transcripts and no sign that half the data
    went somewhere else. The user who wants both picks both, which says which
    is which.
    """
    parent = tmp_path / "both"
    _xenium_run(parent / "run_a")
    _xenium_run(parent / "run_b")

    proposal = inspect_paths([str(parent)])

    assert proposal.samples == []
    assert proposal.unrecognised[0]["path"] == str(parent)


def _boundaries(path, *, count=3, label=True):
    """One row per polygon VERTEX, the way a run writes them."""
    import polars as pl

    columns = {
        "cell_id": [f"cell{i}-1" for i in range(count) for _ in range(4)],
        "vertex_x": [float(v) for i in range(count)
                     for v in (i * 10, i * 10 + 8, i * 10 + 8, i * 10)],
        "vertex_y": [float(v) for _ in range(count)
                     for v in (0, 0, 8, 8)],
    }
    if label:
        columns["label_id"] = [i + 1 for i in range(count) for _ in range(4)]
    pl.DataFrame(columns).write_parquet(path)
    return path


def test_the_runs_cell_boundaries_become_its_segmentation_mask(tmp_path):
    """A Xenium run states its segmentation as polygons rather than pixels.
    Claimed as the sample's mask so the viewer draws it the way it draws every
    other segmentation -- Outlines, Filled, colour by whatever the table says
    -- instead of registering a `shapes` layer that nothing renders."""
    pytest.importorskip("polars")
    run = _xenium_run(tmp_path / "run")
    _boundaries(run / "cell_boundaries.parquet")
    _boundaries(run / "nucleus_boundaries.parquet")

    layers = _by_id(_only(inspect_paths([str(run)])))

    assert layers["cell_boundaries"].role == "mask"
    assert layers["cell_boundaries"].modality == "mask"
    assert layers["cell_boundaries"].label == "Cell segmentation"
    # Nuclei stay an ordinary registered layer: the cell is what the feature
    # table counts, and a project has one segmentation.
    assert layers["nucleus_boundaries"].role == "layer"


def test_boundaries_with_no_numeric_id_are_left_as_a_plain_layer(tmp_path):
    """Numbering them here would invent an order the cell table does not
    share, which colours every cell as its neighbour -- silently."""
    pytest.importorskip("polars")
    run = _xenium_run(tmp_path / "run")
    _boundaries(run / "cell_boundaries.parquet", label=False)

    layers = _by_id(_only(inspect_paths([str(run)])))

    assert layers["cell_boundaries"].role == "layer"


def test_a_run_shipping_a_cell_table_proposes_it_as_the_table(tmp_path):
    pytest.importorskip("pyarrow")
    import pyarrow as pa
    import pyarrow.parquet as pq

    run = _xenium_run(tmp_path / "run")
    pq.write_table(pa.table({"cell_id": pa.array([1, 2, 3]),
                             "x_centroid": pa.array([1.0, 2.0, 3.0]),
                             "y_centroid": pa.array([1.0, 2.0, 3.0])}),
                   run / "cells.parquet")
    sample = _only(inspect_paths([str(run)]))
    cells = _by_id(sample)["cells"]
    # `table`, not a spatial layer: it becomes the sample's DataSpec, which is
    # a different place in the record and a different thing to the viewer.
    assert (cells.kind, cells.role) == ("table", "table")


def test_an_expression_matrix_is_recorded_and_said_to_be_unread(tmp_path):
    """Said out loud rather than silently dropped. A run imported with its
    matrix ignored is a sample whose marker tools are mysteriously empty."""
    run = _xenium_run(tmp_path / "run")
    (run / "cell_feature_matrix.h5").write_bytes(b"\x89HDF\r\n\x1a\n")
    sample = _only(inspect_paths([str(run)]))
    matrix = _by_id(sample)["expression"]
    assert matrix.role == "note"
    assert "not read yet" in matrix.detail


# -- single files ----------------------------------------------------------

def test_an_image_and_its_mask_group_into_one_sample(tmp_path):
    """`slide.ome.tif` and `slide_mask.tif`: the mask's name is a statement
    about the slide, not a different slide."""
    _image(tmp_path / "slide.ome.tif")
    tifffile.imwrite(tmp_path / "slide_mask.tif",
                     (np.arange(256 * 256).reshape(256, 256) % 300).astype(np.uint32))
    sample = _only(inspect_paths([str(tmp_path / "slide.ome.tif"),
                                  str(tmp_path / "slide_mask.tif")]))
    assert sample.name == "slide"
    roles = {layer.role for layer in sample.layers}
    assert roles == {"image", "mask"}


def test_two_slides_with_different_stems_ask_once_and_default_to_two(tmp_path):
    """The only genuinely ambiguous grouping, and the only one asked about.

    Two slides in a folder are usually two slides, so that is the default --
    and the question is there because sometimes they are two rounds of one.
    """
    _image(tmp_path / "slide_a.ome.tif")
    _image(tmp_path / "slide_b.ome.tif")
    proposal = inspect_paths([str(tmp_path / "slide_a.ome.tif"),
                              str(tmp_path / "slide_b.ome.tif")])
    assert len(proposal.samples) == 2
    ids = {q.id for sample in proposal.samples for q in sample.questions}
    assert "images-grouping" in ids

    together = inspect_paths([str(tmp_path / "slide_a.ome.tif"),
                              str(tmp_path / "slide_b.ome.tif")],
                             answers={"images-grouping": "layers"})
    sample = _only(together)
    assert len(sample.layers) == 2
    # One reference; the other is a registered layer, because one sample has
    # one coordinate system.
    assert sum(1 for layer in sample.layers if layer.reference) == 1
    assert {layer.role for layer in sample.layers} == {"image", "layer"}


def test_a_loose_focus_stack_says_which_plane_it_kept(tmp_path):
    """Not only inside a run. Somebody who picks `morphology.ome.tif` on its
    own gets the same reading and the same sentence -- one channel, because
    thirteen other depths were set aside."""
    path = tmp_path / "stack.ome.tif"
    tifffile.imwrite(path, np.zeros((9, 64, 64), dtype=np.uint16),
                     ome=True, metadata={"axes": "ZYX"})

    layer = _by_id(_only(inspect_paths([str(path)])))["stack"]
    assert layer.geometry["numChannels"] == 1
    assert "middle of 9 focal planes" in layer.detail


def test_a_lone_single_plane_uint8_tiff_asks_what_it_is(tmp_path):
    """The one case the file itself cannot settle: a small 8-bit single-plane
    image is both a mask and a grayscale photograph, and guessing wrong either
    way produces something that looks like a bug rather than a decision."""
    tifffile.imwrite(tmp_path / "plate.tif",
                     np.full((64, 64), 3, dtype=np.uint8))
    sample = _only(inspect_paths([str(tmp_path / "plate.tif")]))
    question = sample.questions[0]
    assert question.id.startswith("mask-or-image:")
    assert question.default == "image"

    answered = _only(inspect_paths([str(tmp_path / "plate.tif")],
                                   answers={question.id: "mask"}))
    assert answered.layers[0].role == "mask"


def test_a_name_hint_never_overrules_the_pixels(tmp_path):
    """A three-sample RGB file called `mask.tif` is not a mask, whatever it is
    called: the hint is a tie-break, not evidence."""
    tifffile.imwrite(tmp_path / "mask.tif",
                     np.zeros((64, 64, 3), dtype=np.uint8), photometric="rgb")
    assert import_proposal.looks_like_label_image(tmp_path / "mask.tif") is False


def test_transcripts_alone_get_a_frame_the_size_of_the_data(tmp_path):
    """A sample with no conventional image. The frame's extent comes from the
    parquet's own row-group statistics -- a footer read -- because the frame IS
    the coordinate system every layer is registered against."""
    pytest.importorskip("pyarrow")
    write_transcripts_parquet(tmp_path / "transcripts.parquet", n=1000,
                              width=2000, height=1500)
    sample = _only(inspect_paths([str(tmp_path / "transcripts.parquet")]))
    # The extent of the DATA, not a nominal size: the furthest transcript is a
    # little inside the region it was drawn from, and the frame is what the
    # file actually covers.
    assert 1900 < sample.frame["width"] <= 2001
    assert 1400 < sample.frame["height"] <= 1501
    assert sample.layers[0].modality == "transcripts"
    # Nothing to register against a frame built from its own extent.
    assert sample.layers[0].transform is None


# -- honesty ---------------------------------------------------------------

def test_an_unreadable_folder_is_reported_not_raised(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "readme.txt").write_text("nothing here", encoding="utf-8")
    proposal = inspect_paths([str(tmp_path / "notes")])
    assert proposal.samples == []
    assert proposal.unrecognised[0]["path"].endswith("notes")
    assert proposal.importable is False


def test_one_unreadable_file_does_not_stop_the_others(tmp_path):
    _image(tmp_path / "slide.ome.tif")
    (tmp_path / "notes.md").write_text("hello", encoding="utf-8")
    proposal = inspect_paths([str(tmp_path / "slide.ome.tif"),
                              str(tmp_path / "notes.md")])
    assert proposal.importable is True
    assert len(proposal.unrecognised) == 1


def test_a_plugin_detector_runs_after_the_ladder(tmp_path, monkeypatch):
    """The hook that keeps the next vendor format out of core's importer.

    Called only for a path core did not recognise, so a detector cannot
    accidentally claim an OME-TIFF.
    """
    seen = []

    def detector(path, ctx):
        seen.append(str(path))
        if str(path).endswith(".cosmx"):
            return [import_proposal.LayerProposal(
                id="cosmx", kind="points", modality="cosmx",
                label="CosMx", src=str(path), detail="CosMx run")]
        return None

    monkeypatch.setattr(import_proposal, "_DETECTORS", [detector])
    _image(tmp_path / "slide.ome.tif")
    (tmp_path / "run.cosmx").write_text("x", encoding="utf-8")

    proposal = inspect_paths([str(tmp_path / "slide.ome.tif"),
                              str(tmp_path / "run.cosmx")])
    sample = _only(proposal)
    assert "cosmx" in _by_id(sample)
    # The recognised image never reached the detector.
    assert all(path.endswith(".cosmx") for path in seen)


def test_a_layer_id_is_not_the_double_extension(tmp_path):
    """`slide.ome.tif` is the slide called "slide". The id appears in urls, in
    `ctx.layers.find` and on the card."""
    _image(tmp_path / "slide.ome.tif")
    sample = _only(inspect_paths([str(tmp_path / "slide.ome.tif")]))
    assert sample.layers[0].id == "slide"


def test_a_proposal_serializes_whole(tmp_path):
    """It crosses to the browser as JSON, so every field has to survive."""
    run = _xenium_run(tmp_path / "run")
    payload = inspect_paths([str(run)]).to_dict()
    assert json.loads(json.dumps(payload)) == payload


def test_a_boundary_parquet_on_its_own_is_offered_as_a_mask(tmp_path):
    """Picked from "+ Add Layer" or dropped on the import dialog. Before
    Plexora could draw one this was an unrecognised file; now it is a
    segmentation, and saying so is what lets somebody re-attach a run's
    boundaries to a sample that already exists."""
    pytest.importorskip("polars")
    table = _boundaries(tmp_path / "cell_boundaries.parquet")

    sample = _only(inspect_paths([str(table)]))
    layer = _by_id(sample)["cell_boundaries"]

    assert layer.role == "mask"
    assert layer.kind == "shapes"


def test_vertex_columns_are_never_read_as_cell_centroids(tmp_path):
    """`vertex_x`/`vertex_y` are exactly the names the role guesser reads as
    coordinates, so the boundary test has to come first -- otherwise a
    segmentation registers as a cell table with one row per polygon vertex."""
    pytest.importorskip("polars")
    table = _boundaries(tmp_path / "cell_boundaries.parquet")

    layer = _by_id(_only(inspect_paths([str(table)])))["cell_boundaries"]

    assert layer.role != "table"

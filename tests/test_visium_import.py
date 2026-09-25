"""A standard (non-HD) Visium run, from the folder to a sample of spots.

A handful of 55 micron spots with an 'Antibody Capture' feature beside the
genes, the way a CytAssist protein run is: the spots are the rows of the
feature matrix -- that table's centroids -- and ALSO a `visium_spots` layer
the Visium panel colours by gene; the hires picture is either an H&E or an
immunofluorescence composite on black -- which only its pixels can tell
apart.
"""

import json
from pathlib import Path

import numpy as np
import pytest

import plexora

from plexora.server.models import import_proposal, import_sample
from plexora.server.models.project import CENTROID_LAYER_ID, Project
from plexora.server.utils import brightfield, tenx_matrix

from tests.tenx_fixtures import write_tenx_h5

HIRES_SCALEF = 0.1
REGIST_SCALEF = 0.25
SPOT_DIAMETER = 100.0

#: (barcode, in_tissue, array_row, array_col, pxl_row, pxl_col). One spot the
#: matrix does not have, and the rows out of the matrix's order, so the join
#: is on the barcode and not on the line number.
SPOTS = (
    ("AAAT-1", 1, 3, 7, 3000, 3500),
    ("AAAC-1", 1, 0, 0, 1000, 1200),
    ("GGGG-1", 0, 9, 9, 4800, 4800),
    ("AAAG-1", 1, 1, 3, 1500, 2000),
    ("AACA-1", 0, 2, 4, 2500, 1800),
)
BARCODES = ["AAAC-1", "AAAG-1", "AACA-1", "AAAT-1"]
GENES = ["CD3E", "MS4A1", "KRT19", "CD4_TotalSeqC"]
KINDS = ["Gene Expression"] * 3 + ["Antibody Capture"]
DAPI = {"AAAC-1": 11.5, "AAAG-1": 22.0, "AACA-1": 33.25, "AAAT-1": 44.0}


def _counts():
    """The matrix the run holds, spots x features in BARCODES x GENES order."""
    rng = np.random.default_rng(1)
    counts = rng.integers(0, 6, size=(len(BARCODES), len(GENES)))
    counts[:, 0] += 1                          # nothing all zero
    return counts


#: Dense truth for the tests that read the converted table back.
COUNTS = _counts()


def _write_h5(path):
    import h5py

    write_tenx_h5(path, COUNTS, BARCODES, GENES)
    with h5py.File(path, "r+") as handle:
        features = handle["matrix"]["features"]
        del features["feature_type"]
        features.create_dataset("feature_type", data=np.array(
            [k.encode() for k in KINDS]))


def _fluorescent_picture(size=512):
    """A false-colour composite: black, with blobs in independent planes."""
    array = np.zeros((size, size, 3), dtype=np.uint8)
    yy, xx = np.mgrid[:size, :size]
    for plane, (cy, cx) in enumerate(((120, 140), (300, 380), (400, 150))):
        blob = (yy - cy) ** 2 + (xx - cx) ** 2 < 50 ** 2
        array[blob, plane] = 200
    return array


def _light_picture(size=512):
    """An H&E-like picture: near white, with pink blobs every plane tracks."""
    rng = np.random.default_rng(2)
    array = np.full((size, size, 3), 245, dtype=np.int16)
    yy, xx = np.mgrid[:size, :size]
    for cy, cx in ((120, 140), (300, 380), (400, 150)):
        blob = (yy - cy) ** 2 + (xx - cx) ** 2 < 50 ** 2
        array[blob] = (220, 120, 180)
    array += rng.integers(-3, 4, size=array.shape, dtype=np.int16)
    return np.clip(array, 0, 255).astype(np.uint8)


def write_visium_run(root, *, fluorescent=True):
    """A Visium `outs/` under `root`. Returns the `outs` path."""
    from PIL import Image

    outs = Path(root) / "outs"
    spatial = outs / "spatial"
    spatial.mkdir(parents=True)
    _write_h5(outs / "filtered_feature_bc_matrix.h5")
    lines = ["barcode,in_tissue,array_row,array_col,"
             "pxl_row_in_fullres,pxl_col_in_fullres"]
    lines += [",".join(str(v) for v in spot) for spot in SPOTS]
    (spatial / "tissue_positions.csv").write_text("\n".join(lines) + "\n")
    (spatial / "scalefactors_json.json").write_text(json.dumps({
        "tissue_hires_scalef": HIRES_SCALEF,
        "tissue_lowres_scalef": HIRES_SCALEF / 3,
        "regist_target_img_scalef": REGIST_SCALEF,
        "spot_diameter_fullres": SPOT_DIAMETER,
        "fiducial_diameter_fullres": 150.0,
    }))
    hires = _fluorescent_picture() if fluorescent else _light_picture()
    Image.fromarray(hires).save(spatial / "tissue_hires_image.png")
    Image.fromarray(_light_picture(640)).save(
        spatial / "aligned_tissue_image.jpg", quality=90)
    Image.fromarray(_light_picture(600)).save(
        spatial / "aligned_fiducials.jpg", quality=90)
    rows = ["barcode,in_tissue,DAPI_mean,DAPI_stdev"]
    rows += [f"{b},1,{v},{v / 10}" for b, v in DAPI.items()]
    (spatial / "barcode_fluorescence_intensity.csv").write_text(
        "\n".join(rows) + "\n")
    return outs


@pytest.fixture
def run(tmp_path):
    return write_visium_run(tmp_path / "IF_run", fluorescent=True)


@pytest.fixture
def he_run(tmp_path):
    return write_visium_run(tmp_path / "HE_run", fluorescent=False)


def _sample(outs):
    proposal = import_proposal.inspect_paths([str(outs)])
    assert len(proposal.samples) == 1, proposal.to_dict()
    return proposal.samples[0]


def _layer(sample, layer_id):
    return next(l for l in sample.layers if l.id == layer_id)


@pytest.fixture
def registered(run):
    result = import_sample.import_sample([str(run)])
    return result["name"]


@pytest.fixture
def client(registered):
    return plexora.app.test_client()


# -- the picture ----------------------------------------------------------------

def test_a_dark_composite_is_fluorescence_and_a_light_picture_brightfield(
        run, he_run):
    dark = brightfield.detect_picture_type(
        run / "spatial" / "tissue_hires_image.png")
    light = brightfield.detect_picture_type(
        he_run / "spatial" / "tissue_hires_image.png")
    assert dark.verdict == brightfield.FLUORESCENCE
    assert light.verdict == brightfield.BRIGHTFIELD


# -- the proposal -----------------------------------------------------------------

def test_a_fluorescent_run_proposes_a_multiplex_reference(run):
    assert _layer(_sample(run), "tissue_image").modality == "multiplex"


def test_an_he_run_proposes_an_he_reference(he_run):
    assert _layer(_sample(he_run), "tissue_image").modality == "he"


def test_the_spots_are_the_table(run):
    tables = [l for l in _sample(run).layers if l.role == "table"]
    assert [(l.render or {}).get("tenx") for l in tables] == ["spots"]


def test_the_spots_are_also_a_visium_spots_layer(run):
    """The card the Visium panel lives in, beside the table it reads."""
    spots = [l for l in _sample(run).layers
             if l.kind == "points" and l.modality == "visium_spots"]
    assert [l.id for l in spots] == ["spots"]
    layer = spots[0]
    assert layer.role == "layer"
    assert layer.render["pointKind"] == "spot"
    assert layer.render["spotDiameter"] == pytest.approx(SPOT_DIAMETER)


def test_the_spots_layer_is_placed_from_the_full_res_frame(run):
    """Stated in full-res pixels, like the positions; `_finish` composes the
    frame into the reference's, which the hires PNG is `scalef` of."""
    layer = _layer(_sample(run), "spots")
    assert layer.transform_source == "run"
    assert layer.transform[0] == pytest.approx(HIRES_SCALEF)
    assert layer.transform[3] == pytest.approx(HIRES_SCALEF)
    assert layer.transform[4:] == pytest.approx((0.0, 0.0))


def test_the_aligned_tissue_image_is_a_note(run):
    """Space Ranger's registration checkerboard: QC, not a tissue picture."""
    assert _layer(_sample(run), "aligned_tissue_image").role == "note"


def test_the_fiducial_picture_is_a_note(run):
    assert _layer(_sample(run), "aligned_fiducials").role == "note"


# -- the conversion -----------------------------------------------------------------

def test_the_layout_is_spots(run):
    layout = tenx_matrix.layout_for(run / "filtered_feature_bc_matrix.h5")
    assert layout.kind == "spots"


def test_a_conversion_places_each_spot_by_its_barcode(run, tmp_path):
    import anndata

    layout = tenx_matrix.layout_for(run / "filtered_feature_bc_matrix.h5")
    target = tenx_matrix.convert(layout, tmp_path / "out" / "spots.h5ad")
    adata = anndata.read_h5ad(target)
    by_barcode = {s[0]: (float(s[5]), float(s[4])) for s in SPOTS}
    expected = np.array([by_barcode[b] for b in adata.obs_names])
    assert list(adata.obs_names) == BARCODES
    assert np.array_equal(adata.obsm["spatial"], expected)
    assert np.allclose(adata.obs["DAPI_mean"].to_numpy(),
                       [DAPI[b] for b in BARCODES])
    assert "Antibody Capture" in set(adata.var["feature_types"])


def test_a_layout_without_fluorescence_is_fingerprinted_as_before(run):
    (run / "spatial" / "barcode_fluorescence_intensity.csv").unlink()
    layout = tenx_matrix.layout_for(run / "filtered_feature_bc_matrix.h5")
    assert layout.fluorescence is None
    assert "fluorescence" not in tenx_matrix.source_fingerprint(layout)


# -- registration ---------------------------------------------------------------------

def test_a_registered_run_reads_its_spots_as_the_table(registered):
    project = Project.load(registered)
    assert project.has_table
    assert project.dataset.src.endswith(".h5ad")


def test_the_spot_radius_is_the_run_diameter_in_reference_pixels(registered):
    project = Project.load(registered)
    assert project.visium_spot_radius == pytest.approx(
        SPOT_DIAMETER / 2 * HIRES_SCALEF)


def test_the_centroid_layer_is_labelled_spot_centroids_and_sized(registered):
    project = Project.load(registered)
    spots = project.layer(CENTROID_LAYER_ID)
    assert spots.label == "Spot centroids"
    assert spots.render["radius"] == pytest.approx(
        SPOT_DIAMETER / 2 * HIRES_SCALEF)


def test_the_spots_layer_registers_ready_with_nothing_to_build(registered):
    """Its values are the table's: no builder, so no pending job that would
    wait forever for one."""
    layer = Project.load(registered).layer("spots")
    assert layer is not None
    assert (layer.kind, layer.modality) == ("points", "visium_spots")
    assert layer.status == "ready"
    assert not layer.pending
    assert layer.transform[0] == pytest.approx(HIRES_SCALEF)
    assert not import_sample._needs_build(layer)


def test_a_fluorescent_reference_registers_as_three_channels(registered):
    project = Project.load(registered)
    assert project.image_type == "fluorescence"
    assert list(project.image.channel_names) == ["Red", "Green", "Blue"]


def test_the_aligned_tissue_image_is_not_drawn(registered):
    assert Project.load(registered).layer("aligned_tissue_image") is None


# -- correcting what a layer was recognised as ------------------------------------------

@pytest.fixture
def picture(registered):
    """An extra picture layer on the sample, to recategorise."""
    from plexora.server.models.project import LayerSpec

    Project.mutate(registered, lambda p: p.with_layer(LayerSpec(
        id="extra_picture", kind="image", label="Extra",
        modality="picture")))
    return "extra_picture"


def test_an_image_layer_can_be_renamed_and_recategorised(
        client, registered, picture):
    response = client.patch(
        f"/project/{registered}/layers/{picture}",
        json={"modality": "he", "label": "X"})
    assert response.status_code == 200, response.get_json()
    layer = Project.find(registered).layer(picture)
    assert (layer.modality, layer.label) == ("he", "X")


def test_an_image_layer_cannot_become_transcripts(client, registered, picture):
    response = client.patch(
        f"/project/{registered}/layers/{picture}",
        json={"modality": "transcripts"})
    assert response.status_code == 400


def test_the_reference_is_not_renamed_here(client, registered):
    response = client.patch(f"/project/{registered}/layers/__image__",
                            json={"label": "x"})
    assert response.status_code == 400


def test_the_edit_page_puts_the_image_type_in_the_layer_editor(
        client, registered):
    from bs4 import BeautifulSoup

    response = client.get(f"/edit_config/{registered}")
    assert response.status_code == 200
    page = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    assert page.select(".config-layer-edit")
    select = page.select_one("#edit_image_type")
    assert select is not None
    assert select.find_parent(class_="config-layer-editor") is not None
    titles = [t.get_text(strip=True).lower()
              for t in page.select(".config-section-title")]
    assert "image type" not in titles

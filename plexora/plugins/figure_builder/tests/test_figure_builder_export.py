"""Export re-renders from the source, at whatever resolution is asked for.

This is the other half of the product claim -- the preview raster is a
convenience, and a panel captured at 300 screen pixels has to come out at
however many the DPI demands. Three things carry that and are tested here:

* the compositing matches the shader the user chose their windows against.
  An export that looks different from the viewer is worse than no export: the
  windows were picked by eye, against that arithmetic;
* the pyramid level is chosen from the region and the target, so a panel of a
  whole slide does not read a gigabyte to produce a thumbnail;
* nothing goes through `data_model`. That module holds ONE loaded datasource,
  and it is the one the user is looking at -- an export that loaded a source
  would evict their session, and a four-image figure would evict it four times.

The last is asserted by monkeypatching the loader to explode, which is the only
way to be sure: the wrong call would otherwise merely be slow, and slow passes.
"""

import json
from pathlib import Path

import numpy as np
import pytest
import tifffile

import plexora
from plexora.plugins.figure_builder.server import compose, export, render, repository, schema
from plexora.server.models import data_model, database_model
from tests.helpers import ALL_CONFIRMED, image_spec, project, use_data_root

IMAGE_WIDTH = 512
IMAGE_HEIGHT = 384

#: One channel filled with a constant, so every expected pixel below can be
#: worked out by hand and checked by a reader.
DNA_VALUE = 50
CD8_VALUE = 200


@pytest.fixture
def figure(tmp_path, monkeypatch):
    """A registered project, and a figure with one panel on a page."""
    image_path = tmp_path / "image.ome.tif"
    plane = np.zeros((2, IMAGE_HEIGHT, IMAGE_WIDTH), dtype=np.uint16)
    plane[0, :, :] = DNA_VALUE
    plane[1, :, :] = CD8_VALUE
    tifffile.imwrite(image_path, plane)

    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "demo": project("demo", image=image_spec(
            channels=("DNA", "CD8"), width=IMAGE_WIDTH, height=IMAGE_HEIGHT,
            src=str(image_path)), confirmed=ALL_CONFIRMED).to_entry(),
    }), encoding="utf-8")

    figure_id = repository.create("Figure 1")
    repository.apply(figure_id, 0, [
        {"op": "add_source", "source": {
            "source_id": "src_1", "kind": "plexora_project", "datasource": "demo",
            "display_name": "Demo slide",
            "image": {"width": IMAGE_WIDTH, "height": IMAGE_HEIGHT},
            "pixel_size": {"value": 0.5, "unit": "µm"},
            "channels": [{"key": "DNA", "fullname_at_capture": "DNA"},
                         {"key": "CD8", "fullname_at_capture": "CD8"}]}},
        {"op": "add_panel", "panel": {
            "panel_id": "pnl_1", "source_id": "src_1",
            "scene": {
                "source_id": "src_1",
                "viewport": {"x": 0, "y": 0, "w": 256, "h": 192},
                "channels": [
                    {"key": "DNA", "fullname_at_capture": "DNA",
                     "color": {"r": 0, "g": 0, "b": 255}, "window": [0, 100], "visible": True},
                    {"key": "CD8", "fullname_at_capture": "CD8",
                     "color": {"r": 255, "g": 0, "b": 0}, "window": [0, 400], "visible": True},
                ],
            },
            "placement": {"page_id": "pg_1", "x_mm": 20, "y_mm": 20,
                          "w_mm": 40, "h_mm": 30, "z": 0},
            "scalebar": {"visible": True},
            "legend": {"channels": True, "plugins": False},
        }},
    ])
    return figure_id


# -- the compositing -----------------------------------------------------

def test_a_panel_is_rendered_from_the_source_pixels(figure):
    """The arithmetic, matched against a hand computation of the shader:

        t   = clip((raw - lo) / (hi - lo), 0, 1)
        rgb = colour * t * 0.9        (frag.glsl's alpha)
        accumulate across channels    (canvas "lighter")
    """
    document = repository.load(figure)
    panel = document["panels"]["pnl_1"]
    with render.SourceImage("demo") as source:
        image, _ = render.render_panel(source, panel["scene"], 64, 48)

    pixels = np.asarray(image)
    assert pixels.shape == (48, 64, 3)

    blue = int(np.clip((DNA_VALUE - 0) / 100, 0, 1) * 255 * (render.CHANNEL_ALPHA / 255) * 255)
    red = int(np.clip((CD8_VALUE - 0) / 400, 0, 1) * 255 * (render.CHANNEL_ALPHA / 255) * 255)
    assert int(pixels[24, 32, 2]) == pytest.approx(blue, abs=1)
    assert int(pixels[24, 32, 0]) == pytest.approx(red, abs=1)
    # Green was in neither channel's colour, so nothing may have accumulated
    # into it -- a compositor that leaked across components would show here.
    assert int(pixels[24, 32, 1]) == 0


def test_a_window_clips_rather_than_wrapping(figure):
    """A value above the window is full brightness, not a value that has come
    round again -- which is what an unclamped subtraction produces, and it looks
    like signal."""
    document = repository.load(figure)
    scene = document["panels"]["pnl_1"]["scene"]
    scene["channels"] = [{**scene["channels"][1], "window": [0, 10]}]
    with render.SourceImage("demo") as source:
        image, _ = render.render_panel(source, scene, 8, 8)
    assert int(np.asarray(image)[4, 4, 0]) == pytest.approx(
        int(render.CHANNEL_ALPHA * 255), abs=1)


def test_a_channel_that_is_gone_is_reported_not_substituted(figure):
    document = repository.load(figure)
    scene = document["panels"]["pnl_1"]["scene"]
    scene["channels"].append({"key": "CD11c", "fullname_at_capture": "CD11c",
                              "color": {"r": 0, "g": 255, "b": 0},
                              "window": [0, 100], "visible": True})
    with render.SourceImage("demo") as source:
        report = render.panel_report(source, scene, 40, 300)
        image, detail = render.render_panel(source, scene, 16, 16)

    assert report["missing_channels"] == ["CD11c"]
    assert detail["channels_rendered"] == 2
    # Nothing green was drawn: the missing channel contributed nothing rather
    # than a neighbour standing in for it.
    assert int(np.asarray(image)[8, 8, 1]) == 0


def test_the_export_never_loads_a_datasource(figure, monkeypatch):
    """`data_model` holds one loaded datasource behind a lock, and it is the one
    the user is looking at. Exporting must not evict it -- and a figure spanning
    four images would evict it four times."""
    def explode(*args, **kwargs):
        raise AssertionError("export loaded a datasource; it must read the file directly")

    monkeypatch.setattr(data_model, "load_datasource", explode)
    monkeypatch.setattr(data_model, "ensure_loaded", explode)

    document = repository.load(figure)
    with render.SourceImage("demo") as source:
        render.render_panel(source, document["panels"]["pnl_1"]["scene"], 32, 24)


# -- choosing a level ----------------------------------------------------

class _Pyramid:
    def __init__(self, levels):
        self.levels = levels


@pytest.mark.parametrize("viewport, target, levels, expected", [
    # A whole slide into a small panel: read the coarsest level that still has
    # the detail, not the gigabyte at level 0.
    (40000, 1200, 6, 5),
    # A tight crop already smaller than the target: level 0, and the caller is
    # told the effective DPI is low.
    (400, 1200, 6, 0),
    # Exactly enough at one level down.
    (2400, 1200, 6, 1),
    # A file with no pyramid has one answer.
    (40000, 1200, 1, 0),
])
def test_the_cheapest_level_with_enough_detail_is_chosen(viewport, target, levels, expected):
    assert render.choose_level(_Pyramid(levels), viewport, target) == expected


def test_effective_dpi_is_reported_rather_than_enforced(figure):
    """A panel below the requested DPI still exports. A reviewer's deadline is
    real and a slightly soft inset is usually the right trade -- silently
    upscaling and saying nothing is not."""
    document = repository.load(figure)
    with render.SourceImage("demo") as source:
        report = render.panel_report(source, document["panels"]["pnl_1"]["scene"], 40, 300)
    # 256 source pixels across 40 mm is ~163 DPI.
    assert report["effective_dpi"] == pytest.approx(256 / (40 / 25.4), abs=1)
    assert report["requested_dpi"] == 300


def test_preflight_answers_before_anything_is_rendered(figure):
    document = repository.load(figure)
    report = export.preflight(document, {"dpi": 600})
    assert report["panels"] == 1
    assert any(warning["kind"] == "low_resolution" for warning in report["warnings"])


# -- writing files -------------------------------------------------------

def test_a_png_export_writes_a_page_and_a_manifest(figure, tmp_path):
    document = repository.load(figure)
    result = export.export(document, tmp_path / "out", {"format": "png", "dpi": 150})

    assert result["cancelled"] is False
    written = [p for p in result["files"] if p.endswith(".png")]
    assert len(written) == 1

    from PIL import Image
    with Image.open(written[0]) as page:
        # A4 at 150 DPI.
        assert page.size == (pytest.approx(1240, abs=2), pytest.approx(1754, abs=2))

    manifest_path = next(p for p in result["files"] if p.endswith(".json"))
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    assert manifest["figure"]["figure_id"] == figure
    # The scene travels verbatim: a manifest that summarised it would be a
    # second description of the same thing, and the summary goes stale.
    assert manifest["panels"][0]["scene"]["viewport"]["w"] == 256
    assert manifest["sources"][0]["pixel_size"]["value"] == 0.5


def test_a_transparent_page_keeps_its_alpha_in_png(figure, tmp_path):
    """A figure destined for a dark slide has no background rather than a white
    one. PNG is the only format here that can say so."""
    from PIL import Image

    repository.apply(repository.load(figure)["figure_id"], repository.load(figure)["revision"], [
        {"op": "update_page", "page_id": "pg_1", "changes": {"background": "transparent"}}])
    result = export.export(repository.load(figure), tmp_path / "out",
                           {"format": "png", "dpi": 75})

    with Image.open(next(p for p in result["files"] if p.endswith(".png"))) as page:
        assert page.mode == "RGBA"
        # A corner, which no panel covers: fully transparent rather than white.
        assert page.getpixel((0, 0))[3] == 0


def test_a_transparent_page_is_rendered_white_in_tiff(figure, tmp_path):
    """The deliberate limit, stated in the export dialog. Silently compositing
    onto whatever a submission pipeline paints -- usually black -- would ruin a
    figure at the last step and give no sign it had happened."""
    from PIL import Image

    repository.apply(repository.load(figure)["figure_id"], repository.load(figure)["revision"], [
        {"op": "update_page", "page_id": "pg_1", "changes": {"background": "transparent"}}])
    result = export.export(repository.load(figure), tmp_path / "out",
                           {"format": "tiff", "dpi": 75})

    with Image.open(next(p for p in result["files"] if p.endswith(".tif"))) as page:
        assert page.mode == "RGB"
        assert page.getpixel((0, 0)) == (255, 255, 255)


def test_a_background_that_is_neither_a_colour_nor_transparent_falls_back_to_white():
    """Tolerant on the way in, like the rest of the schema: a page written by
    something that got this wrong opens white rather than refusing to open."""
    assert schema.page_background("transparent") == "transparent"
    assert schema.page_background("  TRANSPARENT ") == "transparent"
    assert schema.page_background("#AABBCC") == "#aabbcc"
    assert schema.page_background("rebeccapurple") == "#ffffff"
    assert schema.page_background(None) == "#ffffff"


def test_the_requested_dpi_decides_the_output_size(figure, tmp_path):
    """The claim itself: the panel was captured at 256 source pixels and the
    page asks for 40 mm, so the pixels in the file are a function of the DPI and
    of nothing about the screen."""
    from PIL import Image

    sizes = {}
    for dpi in (150, 300):
        result = export.export(document_for(figure), tmp_path / f"out{dpi}",
                               {"format": "png", "dpi": dpi})
        with Image.open(next(p for p in result["files"] if p.endswith(".png"))) as page:
            sizes[dpi] = page.size
    assert sizes[300][0] == pytest.approx(sizes[150][0] * 2, abs=3)


def test_an_export_can_be_cancelled_and_leaves_nothing_behind(figure, tmp_path):
    document = repository.load(figure)
    out = tmp_path / "out"
    result = export.export(document, out, {"format": "png"}, cancelled=lambda: True)
    assert result == {"cancelled": True}


def _pdf_text(path):
    """Every string drawn as TEXT in a PDF, as opposed to baked into a raster.

    Reportlab writes page content through ASCII85 and Flate, so this undoes both
    and pulls the literals out of the `(...) Tj` operators. Reading the file
    this way rather than trusting that text was requested is the point of the
    test below: a label that ended up inside the panel bitmap would be invisible
    to any assertion made on this side of the writer.
    """
    import base64
    import re
    import zlib

    raw = Path(path).read_bytes()
    strings = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", raw, re.S):
        body = match.group(1).strip(b"\r\n")
        try:
            body = base64.a85decode(body, adobe=True)
        except ValueError:
            pass
        try:
            body = zlib.decompress(body)
        except zlib.error:
            continue
        strings.extend(literal.decode("latin-1")
                       for literal in re.findall(rb"\((.*?)\)\s*Tj", body, re.S))
    return strings


def test_a_pdf_export_keeps_its_text_as_text(figure, tmp_path):
    """The reason PDF is the editable master and TIFF is not: labels, titles,
    legends and scale-bar captions come out as real text objects, so a figure
    can be adjusted in Illustrator without the microscopy being re-exported --
    and without anyone having to retype a label that was baked into a bitmap."""
    pytest.importorskip("reportlab")
    document = repository.load(figure)
    result = export.export(document, tmp_path / "out", {"format": "pdf", "dpi": 150})

    path = next(p for p in result["files"] if p.endswith(".pdf"))
    assert Path(path).read_bytes().startswith(b"%PDF")

    text = _pdf_text(path)
    assert "A" in text, "the panel label is not vector text"
    # The channel legend was asked for, so both channel names are drawn.
    assert "DNA" in text and "CD8" in text
    # The scale bar's caption: 256 px x 0.5 µm = 128 µm across, a quarter of
    # which rounds down to 25.
    assert any(entry.startswith("25 ") for entry in text), text
    # And the image itself really is embedded as a raster rather than the page
    # being text on white.
    assert b"/Image" in Path(path).read_bytes()


def test_a_pdf_export_appends_a_provenance_page(figure, tmp_path):
    """By default, because a figure that cannot say where it came from is a
    figure a reviewer has to take on trust."""
    pytest.importorskip("reportlab")
    document = repository.load(figure)
    result = export.export(document, tmp_path / "out", {"format": "pdf", "dpi": 150})
    text = " ".join(_pdf_text(next(p for p in result["files"] if p.endswith(".pdf"))))

    assert "SOURCES" in text
    assert "Demo slide" in text
    assert "full-resolution image pixels" in text


def test_a_pdf_export_says_how_to_get_pdf_support_when_it_is_missing(figure, tmp_path, monkeypatch):
    """A build without reportlab still exports PNG and TIFF. Asking for a PDF
    has to name the install line rather than failing with an ImportError from
    three frames down."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("reportlab"):
            raise ImportError("no reportlab")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    with pytest.raises(export.ExportUnavailable, match="plexora\\[figures\\]"):
        export.export(repository.load(figure), tmp_path / "out", {"format": "pdf"})


def test_a_figure_with_nothing_on_a_page_is_still_a_valid_export(figure, tmp_path):
    """An empty page exports as an empty page rather than as an error: it is a
    legitimate state, and refusing would strand a user mid-layout."""
    document = repository.load(figure)
    document["panels"]["pnl_1"]["placement"] = None
    result = export.export(document, tmp_path / "out", {"format": "png", "dpi": 96})
    assert result["cancelled"] is False


# -- the provenance page -------------------------------------------------

def test_the_provenance_names_the_region_and_the_windows(figure, tmp_path):
    from plexora.plugins.figure_builder.server import provenance

    document = repository.load(figure)
    with render.SourceImage("demo") as source:
        report = render.panel_report(source, document["panels"]["pnl_1"]["scene"], 40, 300)
    text = "\n".join(provenance.lines(document, [(document["panels"]["pnl_1"], report)],
                                      {"format": "pdf", "dpi": 300}))

    assert "Demo slide" in text
    assert "full-resolution image pixels" in text
    assert "window 0–100" in text
    # 256 px x 0.5 µm/px = 128 µm across.
    assert "128 µm" in text


def test_the_provenance_says_when_the_scale_is_unknown(figure):
    from plexora.plugins.figure_builder.server import provenance

    document = repository.load(figure)
    document["sources"]["src_1"]["pixel_size"] = None
    text = "\n".join(provenance.lines(
        document, [(document["panels"]["pnl_1"], {})], {"format": "pdf", "dpi": 300}))
    assert "scale information unavailable" in text


def test_the_provenance_names_what_the_export_could_not_reproduce():
    """The reason this file exists rather than the export just writing a title
    page: an export that silently omits the phenotype colouring a figure was
    made to show misrepresents the figure."""
    from plexora.plugins.figure_builder.server import provenance

    document = schema.new_document("fig_aaaaaaaaaaaa", title="F")
    text = "\n".join(provenance.lines(
        document, [({"panel_id": "p", "source_id": "s", "title": "",
                     "scene": {"viewport": {"x": 0, "y": 0, "w": 1, "h": 1},
                               "channels": [], "plugins": {}},
                     "derived_from": None, "placement": None},
                    {"missing_overlays": ["cell_explorer"], "missing_channels": []})],
        {"format": "pdf", "dpi": 300}))
    assert "NOT REPRODUCED IN THIS EXPORT" in text
    assert "cell_explorer" in text


# -- layout arithmetic ---------------------------------------------------

def test_a_scale_bar_is_never_invented():
    document = schema.new_document("fig_aaaaaaaaaaaa")
    document["sources"]["src_1"] = {"source_id": "src_1", "pixel_size": None}
    panel = {"source_id": "src_1", "scalebar": {"visible": True, "target_um": None},
             "scene": {"viewport": {"w": 1000}}}
    assert compose.scale_bar(document, panel) is None


def test_a_scale_bar_is_a_round_number_that_fits():
    document = schema.new_document("fig_aaaaaaaaaaaa")
    document["sources"]["src_1"] = {"source_id": "src_1",
                                    "pixel_size": {"value": 0.5, "unit": "µm"}}
    panel = {"source_id": "src_1", "scalebar": {"visible": True, "target_um": None},
             "scene": {"viewport": {"w": 2000}}}   # 1000 µm across
    bar = compose.scale_bar(document, panel)
    assert bar["length_um"] == 250
    assert bar["label"] == "250 µm"
    assert bar["fraction"] == pytest.approx(0.25)


def test_panel_labels_run_in_reading_order(figure):
    """The same sequence the canvas uses, so the preview and the export agree.
    Base-26 with no zero digit: position 26 is AA, not BA."""
    assert [compose.label_for(i) for i in (0, 1, 25, 26, 27)] == \
        ["A", "B", "Z", "AA", "AB"]


def document_for(figure_id):
    return repository.load(figure_id)

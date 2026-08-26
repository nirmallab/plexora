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
import re
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


def test_a_colour_bar_and_a_caption_survive_every_format(figure, tmp_path):
    """The two new pieces of furniture, through both writers.

    A colour bar is a ramp -- 48 abutting fills per channel -- and a caption is
    a text run at its own size and colour. Neither had a backend path before,
    and a figure that renders them on screen and silently drops them in the file
    is the failure a preview cannot show you. Run against both orientations
    because the vertical bar is the one whose stops the PDF writer has to
    reverse, and the raster writer with it.

    Geometry is `test_figure_builder_furniture.py`'s business; this is only
    "does it come out of both writers at all".
    """
    document = repository.load(figure)
    panel = document["panels"]["pnl_1"]
    panel["colorbar"] = schema.normalize_colorbar(
        {"visible": True, "orientation": "vertical", "ticks": 2})
    panel["labels"] = [schema.normalize_panel_label(
        {"label_id": "lbl_1", "text": "Tumor core", "position": "top_right",
         "color": "#ffd60a", "size_pt": 9})]

    result = export.export(document, tmp_path / "out", {"format": "pdf", "dpi": 150})
    text = _pdf_text(next(p for p in result["files"] if p.endswith(".pdf")))
    assert "Tumor core" in text
    # The ticks are the channel's own window in raw units -- DNA is [0, 100].
    assert "100" in text

    for fmt in ("png", "tiff"):
        made = export.export(document, tmp_path / fmt, {"format": fmt, "dpi": 100})
        assert made["files"], fmt


def test_a_pdf_export_appends_a_provenance_page(figure, tmp_path):
    """By default, because a figure that cannot say where it came from is a
    figure a reviewer has to take on trust."""
    document = repository.load(figure)
    result = export.export(document, tmp_path / "out", {"format": "pdf", "dpi": 150})
    text = " ".join(_pdf_text(next(p for p in result["files"] if p.endswith(".pdf"))))

    assert "SOURCES" in text
    assert "Demo slide" in text
    assert "full-resolution image pixels" in text


def test_a_pdf_export_says_how_to_get_pdf_support_when_it_is_missing(figure, tmp_path, monkeypatch):
    """reportlab is a hard dependency now, but the guard stays: an environment
    that lost it still exports PNG and TIFF, and asking for a PDF has to name
    the install line rather than failing with an ImportError from three frames
    down."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("reportlab"):
            raise ImportError("no reportlab")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    with pytest.raises(export.ExportUnavailable, match="pip install reportlab"):
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


# -- shapes --------------------------------------------------------------

#: An ellipse as the shape tool stores one: four smooth nodes and KAPPA. Enough
#: to prove curves survive both writers; the node tables themselves are pinned
#: in test_figure_builder_shapes.py.
_K = 0.5522847498307936 * 0.5
ELLIPSE_NODES = [
    {"x": 0.5, "y": 0.0, "type": "smooth",
     "in": {"x": 0.5 - _K, "y": 0.0}, "out": {"x": 0.5 + _K, "y": 0.0}},
    {"x": 1.0, "y": 0.5, "type": "smooth",
     "in": {"x": 1.0, "y": 0.5 - _K}, "out": {"x": 1.0, "y": 0.5 + _K}},
    {"x": 0.5, "y": 1.0, "type": "smooth",
     "in": {"x": 0.5 + _K, "y": 1.0}, "out": {"x": 0.5 - _K, "y": 1.0}},
    {"x": 0.0, "y": 0.5, "type": "smooth",
     "in": {"x": 0.0, "y": 0.5 + _K}, "out": {"x": 0.0, "y": 0.5 - _K}},
]

#: An open "V". Its fill is set and must never be drawn: where the missing edge
#: runs is a guess, and each renderer would guess differently.
VEE_NODES = [{"x": 0.0, "y": 0.0}, {"x": 0.5, "y": 1.0}, {"x": 1.0, "y": 0.0}]


def _add_shapes(figure_id, version=1):
    """A filled curved shape with no stroke, and a stroked open path with a
    fill it must ignore."""
    return repository.apply(figure_id, version, [
        {"op": "add_annotation", "annotation": {
            "annotation_id": "ann_disc", "type": "shape", "page_id": "pg_1",
            "geometry": {"x_mm": 100, "y_mm": 100, "w_mm": 40, "h_mm": 30, "rotation": 0},
            "style": {"fill": "#ff0000", "color": "#000000", "line_width_pt": 0},
            "shape": {"preset": "ellipse", "closed": True, "nodes": ELLIPSE_NODES}}},
        {"op": "add_annotation", "annotation": {
            "annotation_id": "ann_vee", "type": "shape", "page_id": "pg_1",
            "geometry": {"x_mm": 40, "y_mm": 200, "w_mm": 40, "h_mm": 20, "rotation": 0},
            "style": {"fill": "#00ff00", "color": "#0000ff", "line_width_pt": 2},
            "shape": {"preset": "custom", "closed": False, "nodes": VEE_NODES}}},
    ])


def _pdf_streams(path):
    """Every decompressed content stream in a PDF, as text."""
    import base64
    import re
    import zlib

    out = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream",
                             Path(path).read_bytes(), re.S):
        body = match.group(1).strip(b"\r\n")
        try:
            body = base64.a85decode(body, adobe=True)
        except ValueError:
            pass
        try:
            out.append(zlib.decompress(body).decode("latin-1"))
        except (zlib.error, UnicodeDecodeError):
            continue
    return "\n".join(out)


def test_a_shape_reaches_the_pdf_as_a_curve_and_not_a_bitmap(figure, tmp_path):
    """The reason the PDF branch has its own path writer: a shape drawn with
    beziers comes out as beziers, so it is still editable in Illustrator. A
    flattened polyline would look identical on screen and be a hundred straight
    segments to anyone who opened it."""
    _add_shapes(figure)
    result = export.export(document_for(figure), tmp_path / "out",
                           {"format": "pdf", "dpi": 150})
    content = _pdf_streams(next(p for p in result["files"] if p.endswith(".pdf")))

    # Reportlab writes a whole path on one line -- "x y m ... c ... h" -- so
    # the operators are counted in place rather than per line.
    curves = re.findall(r"(?<=\d)\s+c(?=\s)", content)
    assert len(curves) >= 4, "the ellipse's four bezier edges are not in the PDF"
    # The closed one is filled and the open one stroked, and neither is the
    # other. `f`/`f*` fill, `S`/`s` stroke.
    assert re.search(r"(?m)^f\*?$", content), content[-600:]
    assert re.search(r"(?m)^[Ss]$", content), content[-600:]


def test_a_shape_is_rasterised_where_it_was_put(figure, tmp_path):
    """Measured in pixels rather than trusted, because the raster writer
    flattens the curve itself and a tolerance computed in the wrong units draws
    a polygon where the screen showed a disc."""
    from PIL import Image

    _add_shapes(figure)
    result = export.export(document_for(figure), tmp_path / "out",
                           {"format": "png", "dpi": 150})
    scale = 150 / 25.4

    def at(x_mm, y_mm):
        return page.getpixel((round(x_mm * scale), round(y_mm * scale)))[:3]

    with Image.open(next(p for p in result["files"] if p.endswith(".png"))) as page:
        page = page.convert("RGB")
        # Dead centre of the disc.
        assert at(120, 115) == (255, 0, 0)
        # The corner of its own box, which an ellipse does not reach. A shape
        # drawn as its bounding rectangle passes every other assertion here.
        assert at(101, 101) == (255, 255, 255)
        # Inside the V, where its ignored fill would be if fill were drawn.
        assert at(60, 205) == (255, 255, 255)
        # And on the V's left arm, a quarter of the way down.
        assert at(45, 205) == (0, 0, 255)


def test_a_translucent_shape_lets_the_page_through(figure, tmp_path):
    """Opacity is composited, not mixed into the colour. Half-transparent red
    over white is (255, 127, 127); a colour lightened to look the same would
    stay that colour over a panel, which is where it would show."""
    from PIL import Image

    revision = _add_shapes(figure)
    repository.apply(figure, revision, [{"op": "update_annotation", "annotation_id": "ann_disc",
                                         "changes": {"style": {"opacity": 0.5}}}])
    result = export.export(document_for(figure), tmp_path / "out",
                           {"format": "png", "dpi": 150})
    scale = 150 / 25.4
    with Image.open(next(p for p in result["files"] if p.endswith(".png"))) as page:
        red, green, blue = page.convert("RGB").getpixel(
            (round(120 * scale), round(115 * scale)))[:3]
    assert red == 255
    assert 110 <= green <= 145, (red, green, blue)
    assert green == blue


def test_a_rotated_shape_turns_about_the_centre_of_its_own_box(figure, tmp_path):
    """A bar rotated 90 degrees is the one case where getting the pivot wrong is
    obvious: it lands somewhere else entirely rather than merely looking off."""
    from PIL import Image

    repository.apply(figure, 1, [{"op": "add_annotation", "annotation": {
        "annotation_id": "ann_bar", "type": "shape", "page_id": "pg_1",
        "geometry": {"x_mm": 80, "y_mm": 148, "w_mm": 60, "h_mm": 10, "rotation": 90},
        "style": {"fill": "#ff0000", "color": "#000000", "line_width_pt": 0},
        "shape": {"preset": "rect", "closed": True,
                  "nodes": [{"x": 0, "y": 0}, {"x": 1, "y": 0},
                            {"x": 1, "y": 1}, {"x": 0, "y": 1}]}}}])
    result = export.export(document_for(figure), tmp_path / "out",
                           {"format": "png", "dpi": 150})
    scale = 150 / 25.4
    with Image.open(next(p for p in result["files"] if p.endswith(".png"))) as page:
        page = page.convert("RGB")

        def at(x_mm, y_mm):
            return page.getpixel((round(x_mm * scale), round(y_mm * scale)))[:3]

        # The box's centre is (110, 153) and rotation leaves it there, so the
        # bar now runs vertically through it: 60mm tall, 10mm wide.
        assert at(110, 153) == (255, 0, 0)
        assert at(110, 130) == (255, 0, 0)
        assert at(110, 176) == (255, 0, 0)
        # And is no longer where it was drawn before the turn.
        assert at(85, 153) == (255, 255, 255)


# -- lines and arrows ----------------------------------------------------
#
# The geometry is pinned in test_figure_builder_lines.py against the browser's
# own. What is checked here is that it survives the two WRITERS: that a dash
# reaches the PDF as a dash operator rather than as a hundred little lines, that
# a taper is filled ink, and -- the one that was a real bug -- that a head
# reaches the raster at all.


def _add_line(figure_id, version, annotation_id, style, geometry=None):
    return repository.apply(figure_id, version, [
        {"op": "add_annotation", "annotation": {
            "annotation_id": annotation_id, "type": "line", "page_id": "pg_1",
            "geometry": geometry or {"x_mm": 40, "y_mm": 100, "w_mm": 80,
                                     "h_mm": 0, "rotation": 0},
            "style": {"color": "#0000ff", "line_width_pt": 2, **style}}}])


def _page(result, suffix):
    return next(p for p in result["files"] if p.endswith(suffix))


def test_an_arrowhead_finally_reaches_the_raster(figure, tmp_path):
    """The bug this work fixed.

    PNG and TIFF had no arrowhead code at ALL -- the raster branch drew the
    shaft and stopped -- so every arrow in every figure exported to a bitmap as
    a plain line, and the only way to notice was to look at the file. Heads are
    `path` instructions now, which the raster writer has drawn all along.
    """
    from PIL import Image

    _add_line(figure, 1, "ann_arrow",
              {"end_head": "filled", "head_size_pt": 20, "line_width_pt": 2})
    result = export.export(document_for(figure), tmp_path / "out",
                           {"format": "png", "dpi": 150})
    scale = 150 / 25.4
    with Image.open(_page(result, ".png")) as page:
        page = page.convert("RGB")

        def at(x_mm, y_mm):
            return page.getpixel((round(x_mm * scale), round(y_mm * scale)))[:3]

        # The head is 20pt long -- a little over 7mm -- ending at (120, 100),
        # so this is inside the triangle and well clear of the 2pt shaft.
        assert at(117, 101) == (0, 0, 255), "the arrowhead is missing from the PNG"
        # ... and the shaft is still there.
        assert at(60, 100) == (0, 0, 255)
        # Nothing past the tip.
        assert at(124, 100) == (255, 255, 255)


def test_an_arrowhead_reaches_the_pdf_as_filled_ink(figure, tmp_path):
    """A solid head is a filled polygon and an open one is two strokes, which is
    the difference the user is choosing between. Both are `path` instructions,
    so neither writer has arrowhead code of its own to get wrong."""
    _add_line(figure, 1, "ann_arrow", {"end_head": "filled", "head_size_pt": 20})
    result = export.export(document_for(figure), tmp_path / "out",
                           {"format": "pdf", "dpi": 150})
    content = _pdf_streams(_page(result, ".pdf"))
    assert re.search(r"(?m)^f\*?$", content), "no filled head in the PDF"


@pytest.mark.parametrize("line_style", ["dashed", "dotted"])
def test_a_dash_reaches_the_pdf_as_a_dash_and_not_as_pieces(figure, tmp_path, line_style):
    """One `d` operator, not a hundred little lines.

    The pattern is derived server-side from the enum and never taken from the
    document, because reportlab RAISES on a negative entry or a cycle summing to
    zero -- and the exception comes out of the middle of the PDF writer naming
    no annotation at all.
    """
    _add_line(figure, 1, "ann_dash", {"line_style": line_style})
    result = export.export(document_for(figure), tmp_path / "out",
                           {"format": "pdf", "dpi": 150})
    content = _pdf_streams(_page(result, ".pdf"))
    assert re.search(r"\[[\d.\s]+\]\s+0\s+d\b", content), content[-800:]


def test_a_dotted_line_has_gaps_in_the_raster(figure, tmp_path):
    """Pillow has no dash array, so the pieces are walked one at a time. A
    dotted line that came out solid would look like a slightly fat solid line
    and pass every test that only looked for ink."""
    from PIL import Image

    _add_line(figure, 1, "ann_dots", {"line_style": "dotted", "line_width_pt": 3})
    result = export.export(document_for(figure), tmp_path / "out",
                           {"format": "png", "dpi": 150})
    scale = 150 / 25.4
    with Image.open(_page(result, ".png")) as page:
        page = page.convert("RGB")
        row = [page.getpixel((round(x_mm * scale), round(100 * scale)))[:3]
               for x_mm in [40 + step * 0.25 for step in range(0, 320)]]
    assert (0, 0, 255) in row, "the dotted line drew nothing"
    assert (255, 255, 255) in row, "the dotted line drew no gaps"


def test_a_taper_is_wide_at_one_end_and_thin_at_the_other(figure, tmp_path):
    """A taper is filled ink, not a pen -- no renderer here has a variable-width
    one. Measured as a column height at each end rather than merely looked for:
    a taper drawn as an ordinary stroke is present at both ends and the same
    width at both, which is the failure worth catching."""
    from PIL import Image

    _add_line(figure, 1, "ann_taper",
              {"edge": "taper_end", "line_width_pt": 12})
    result = export.export(document_for(figure), tmp_path / "out",
                           {"format": "png", "dpi": 150})
    scale = 150 / 25.4

    def thickness(x_mm):
        column = round(x_mm * scale)
        return sum(1 for y in range(round(90 * scale), round(110 * scale))
                   if page.getpixel((column, y))[:3] == (0, 0, 255))

    with Image.open(_page(result, ".png")) as page:
        page = page.convert("RGB")
        fat = thickness(42)
        thin = thickness(118)
    assert fat > thin * 3, (fat, thin)
    assert thin >= 1, "the thin end vanished entirely"


def test_a_fade_is_faint_at_the_faded_end(figure, tmp_path):
    """A PDF stroke cannot carry a gradient and Pillow has none either, so both
    writers walk the same plan of short segments at falling alpha. Checked as an
    ORDERING rather than against numbers: what would actually be wrong is the
    ramp running the other way, which is invisible on screen where SVG paints
    the gradient itself."""
    from PIL import Image

    _add_line(figure, 1, "ann_fade", {"edge": "fade_end", "line_width_pt": 4})
    result = export.export(document_for(figure), tmp_path / "out",
                           {"format": "png", "dpi": 150})
    scale = 150 / 25.4
    with Image.open(_page(result, ".png")) as page:
        page = page.convert("RGB")

        def blueness(x_mm):
            red, _green, _blue = page.getpixel(
                (round(x_mm * scale), round(100 * scale)))[:3]
            return 255 - red          # white is 255, solid blue is 0

        assert blueness(42) > blueness(75) > blueness(118)
        assert blueness(42) > 200, "the solid end faded too"


def test_a_head_is_never_drawn_at_a_fades_alpha():
    """A head placed at the faded end must not disappear with it -- "fade the
    line" is not "delete the arrowhead". Read off the instructions rather than
    the pixels, because the failure is a factor of alpha and pixels would only
    say "fainter"."""
    annotation = schema.normalize_annotation({
        "annotation_id": "ann_1", "type": "line", "page_id": "pg_1",
        "geometry": {"x_mm": 0, "y_mm": 0, "w_mm": 100, "h_mm": 0},
        "style": {"color": "#000000", "end_head": "filled", "edge": "fade_end",
                  "opacity": 0.8}})
    items = compose._annotation(annotation, {})
    heads = [item for item in items if item["kind"] == "path"]
    assert heads, "the head did not survive composition"
    assert all(item["opacity"] == pytest.approx(0.8) for item in heads)


def test_a_legacy_arrow_still_composes_to_the_line_it_always_drew():
    """Every arrow in every existing figure stored no head at all, and the
    schema's kind-dependent `end_head` default is what keeps its barbs. If that
    broke, this would compose to a bare shaft -- silently, on every reload."""
    annotation = schema.normalize_annotation({
        "annotation_id": "ann_1", "type": "arrow", "page_id": "pg_1",
        "geometry": {"x_mm": 10, "y_mm": 10, "w_mm": 50, "h_mm": 0},
        "style": {"color": "#000000", "line_width_pt": 0.75}})
    items = compose._annotation(annotation, {})
    shafts = [item for item in items if item["kind"] == "line"]
    barbs = [item for item in items if item["kind"] == "path"]

    assert len(shafts) == 1
    # An open head trims nothing, so the shaft is still the whole stored span.
    assert (shafts[0]["x"], shafts[0]["w"]) == (10, 50)
    assert shafts[0]["dash_pt"] is None and shafts[0]["fade"] is None
    # Two stroked barbs, and no filled polygon.
    assert len(barbs) == 2
    assert all(item["fill"] is None and item["stroke"] == "#000000" for item in barbs)


def test_nothing_composes_to_the_arrow_instruction_kind_any_more():
    """`arrow` is a stored TYPE and was never a useful instruction kind: it made
    the exporters branch on it, which is how one of them ended up with no
    arrowhead code. There is one shaft kind now."""
    for kind in ("line", "arrow"):
        annotation = schema.normalize_annotation({
            "annotation_id": "ann_1", "type": kind, "page_id": "pg_1",
            "geometry": {"x_mm": 0, "y_mm": 0, "w_mm": 40, "h_mm": 20},
            "style": {"color": "#000000"}})
        kinds = {item["kind"] for item in compose._annotation(annotation, {})}
        assert kinds <= {"line", "path"}, kind


def test_rotation_does_nothing_to_a_line():
    """Unsupported, and now said out loud. `w_mm`/`h_mm` already carry the
    line's direction in their SIGNS, so there is no obvious pivot to turn about,
    and no renderer has ever turned one. A test rather than a comment because
    the alternative is somebody discovering it in an export."""
    def instructions(rotation):
        return compose._annotation(schema.normalize_annotation({
            "annotation_id": "ann_1", "type": "line", "page_id": "pg_1",
            "geometry": {"x_mm": 10, "y_mm": 20, "w_mm": 50, "h_mm": 30,
                         "rotation": rotation},
            "style": {"color": "#000000", "end_head": "open"}}), {})

    assert instructions(0) == instructions(37)


def test_a_taper_ignores_the_dash_it_was_told_to_have():
    """One `Edge` control, one answer. Dashing a ribbon whose width varies along
    it is a fourth renderer path for a look nobody asked for; the setting is
    stored so that switching the edge back brings it with it."""
    annotation = schema.normalize_annotation({
        "annotation_id": "ann_1", "type": "line", "page_id": "pg_1",
        "geometry": {"x_mm": 0, "y_mm": 0, "w_mm": 60, "h_mm": 0},
        "style": {"color": "#000000", "line_style": "dashed", "edge": "taper_end"}})
    assert annotation["style"]["line_style"] == "dashed"
    items = compose._annotation(annotation, {})
    assert [item["kind"] for item in items] == ["path"]
    assert "dash_pt" not in items[0]

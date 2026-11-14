"""An image that was meant to go in a `.plexora` file but could not be packed
(server/models/bundles.py: export's "gone before it was packed", and
finish_manifest's left_out from the website's page) is not in the file, so
the bundle must not say it is: its ref is not `included`, the import counts it
as found or missing, and the image is looked for where it is, not pointed at
a copy that never landed."""

from plexora import paths
from plexora.server.models import bundles, sources
from plexora.server.models.project import Project

from tests.test_bundles import _another_computer, _image, _members, _register


def _project(tmp_path):
    image = _image(tmp_path / "here" / "slide.ome.tif")
    _register("slide", image)
    return image


def _export_while_the_image_is_away(image, out, monkeypatch):
    """The image is there when the export plans and gone when it packs (a
    rename on a share, a sync client rewriting it), then back. Windows will
    not rename a file the registered project holds open, so the pack is
    pointed at a name that is not there, which is what it sees then."""
    planned = bundles.plan

    def plan_then_vanish(*args, **kwargs):
        built = planned(*args, **kwargs)
        media = [member for member in built.members if member.bundle_path.startswith(bundles.MEDIA + "/")]
        assert media, "the plan includes the image"
        for member in media:
            member.source = str(image.with_name("slide.ome.tif.away"))
        return built

    monkeypatch.setattr(bundles, "plan", plan_then_vanish)
    try:
        result = bundles.export(["slide"], out, include={"images": True})
    finally:
        monkeypatch.setattr(bundles, "plan", planned)
    assert image.is_file()
    return result


def test_an_image_left_out_of_the_bundle_is_not_said_to_be_included(tmp_path, monkeypatch):
    image = _project(tmp_path)
    out = tmp_path / "slide.plexora"
    _export_while_the_image_is_away(image, out, monkeypatch)
    names, manifest = _members(out)
    assert manifest["media"] == []
    assert any(item["why"] == "gone before it was packed" for item in manifest["left_out"])
    ref = manifest["projects"][0]["refs"][0]
    assert not ref["included"] or ref["included"] in names, \
        f"the ref says {ref['included']} is included, but the bundle has no such member"


def test_an_image_left_out_of_the_bundle_is_found_where_it_is_on_import(tmp_path, monkeypatch):
    image = _project(tmp_path)
    out = tmp_path / "slide.plexora"
    _export_while_the_image_is_away(image, out, monkeypatch)
    # Another computer that sees the image at the same path.
    _another_computer(tmp_path, monkeypatch)
    monkeypatch.setattr(sources, "app_key", lambda: "host:OTHER")
    monkeypatch.setattr(bundles, "FIND_SECONDS", 0.0)
    refs = bundles.preview(out)["projects"][0]["refs"]
    assert refs["included"] == 0 and refs["found"] == 1, refs
    result = bundles.apply(out)
    assert result["projects"][0]["refs"]["included"] == 0, result["projects"][0]["refs"]
    src = Project.load("slide").image.src
    never_placed = paths.project_state_dir("slide") / bundles.MEDIA
    assert not str(src).startswith(str(never_placed)), f"the image points at {src}, a copy that never landed"
    assert src == str(image)

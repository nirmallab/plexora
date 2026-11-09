"""One index of the copies the website's page makes of the user's files.

The page builds a flat image's pyramidized copy and a flat mask's label
pyramid (client services/browserPyramid.js), into a folder the user chose,
Plexora's folder (Documents/Plexora/pyramids/<key>/), the site's own storage
or IndexedDB. Every copy is recorded in one index (services/derivedIndex.js,
tests/js/derived_index_probe.mjs), looked for in every home before anything is
built, and a copy in a folder the browser opens again only after a click is
*waiting*: Python neither rebuilds it nor asks for it to be found
(providers/browser.py `derived_status`). The desktop app finds what the
website built -- in Plexora's folder, or written into the project store --
and adopts it instead of building it again (server/utils/relink.py).
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from plexora.server.models import sources
from plexora.server.providers import browser
from plexora.server.utils import relink

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "derived_index_probe.mjs"


def test_the_page_keeps_one_index_and_one_lookup_order():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run([node, str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=60)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert "derived index probe OK" in done.stdout


@pytest.fixture
def website(monkeypatch):
    monkeypatch.setenv("PLEXORA_BACKEND", "browser")
    yield
    browser.set_asker(None)


def _asker(copies, asked=None):
    def ask(kind, args):
        if asked is not None:
            asked.append(kind)
        assert kind == "derived_index"
        return {"copies": copies}
    return ask


def test_a_copy_waiting_on_a_folder_is_said_so(website):
    browser.set_asker(_asker({"/derived/k/slide.pyramid.ome.tiff": {"state": "waiting", "place": "“data”"}}))
    assert browser.derived_status("/derived/k/slide.pyramid.ome.tiff") == "waiting"
    assert browser.derived_status("/derived/k/other.pyramid.ome.tiff") == "missing"


def test_a_mounted_copy_asks_the_page_nothing(website, monkeypatch, tmp_path):
    asked = []
    browser.set_asker(_asker({}, asked))
    monkeypatch.setattr(browser.os.path, "exists", lambda path: True)
    assert browser.derived_status("/derived/k/slide.pyramid.ome.tiff") == "mounted"
    assert asked == []


def test_off_the_website_nothing_is_a_page_copy(monkeypatch):
    monkeypatch.delenv("PLEXORA_BACKEND", raising=False)
    browser.set_asker(lambda kind, args: pytest.fail("asked the page"))
    try:
        assert browser.derived_status("/derived/k/slide.pyramid.ome.tiff") is None
        assert browser.derived_index() == {}
    finally:
        browser.set_asker(None)


def test_a_page_that_cannot_say_knows_of_nothing_waiting(website):
    def broken(kind, args):
        raise RuntimeError("the page went away")
    browser.set_asker(broken)
    assert browser.derived_status("/derived/k/slide.pyramid.ome.tiff") == "missing"


def test_the_key_is_the_one_the_page_builds_under():
    seed = "slide.tif|10-2000000"
    assert browser.derived_key("slide.tif", "10-2000000") == hashlib.sha256(seed.encode()).hexdigest()[:24]
    assert browser.derived_key("m.tif", "1-2", "labels") == hashlib.sha256(b"labels|m.tif|1-2").hexdigest()[:24]


# -- a mask's pyramid that waits is not rebuilt -------------------------------

def _mask_entry(tmp_path, derived="/derived/k/cells.labels.pyramid.ome.tiff"):
    from plexora.server.utils import segmentation_pyramid

    source = tmp_path / "cells.tif"
    source.write_bytes(b"x" * 100)
    return {"segmentation": derived, "segmentationSource": str(source),
            "segmentationSourceKey": segmentation_pyramid.source_fingerprint(source),
            "segmentationMode": segmentation_pyramid.MODE_FILLED}, source


def test_a_waiting_mask_pyramid_is_current_while_its_mask_is_unchanged(website, tmp_path):
    from plexora.server.models import data_model

    entry, source = _mask_entry(tmp_path)
    browser.set_asker(_asker({entry["segmentation"]: {"state": "waiting"}}))
    assert data_model._segmentation_mapping_is_current(entry) is True
    source.write_bytes(b"y" * 200)  # the mask changed: the copy is stale
    assert data_model._segmentation_mapping_is_current(entry) is False


def test_a_missing_mask_pyramid_is_built_again(website, tmp_path):
    from plexora.server.models import data_model

    entry, _ = _mask_entry(tmp_path)
    browser.set_asker(_asker({}))
    assert data_model._segmentation_mapping_is_current(entry) is False


def test_a_pyramid_beside_the_mask_is_adopted_before_the_page_is_asked(website, monkeypatch):
    from plexora.server.models import data_model
    from plexora.server.utils import segmentation_pyramid

    found = Path("/picked/ab/cells.labels.pyramid.ome.tiff")
    monkeypatch.setattr(data_model, "_resolve_label_volume", lambda *a, **k: None)
    monkeypatch.setattr(data_model, "_servable_as_is", lambda *a, **k: False)
    monkeypatch.setattr(segmentation_pyramid, "resolve_derived_mask",
                        lambda source, directory, mode: segmentation_pyramid.DerivedMask(found, found, False))
    browser.set_asker(lambda kind, args: pytest.fail(f"asked the page ({kind})"))
    path = data_model.resolve_outline_segmentation("/picked/ab/cells.tif", "/data/derived/s",
                                                   mode=segmentation_pyramid.MODE_FILLED)
    assert path == str(found)


def test_an_image_copy_already_beside_it_is_adopted_before_the_page_builds(website, monkeypatch):
    from plexora.server.models import data_model
    from plexora.server.utils import image_pyramid

    found = Path("/picked/ab/slide.pyramid.ome.tiff")
    monkeypatch.setattr(image_pyramid, "flat_image_report", lambda *a, **k: {"channels": 0})
    monkeypatch.setattr(data_model.brightfield, "physical_metadata", lambda path: None)
    monkeypatch.setattr(image_pyramid, "find_derived_image", lambda source, directory: found)
    browser.set_asker(lambda kind, args: pytest.fail(f"asked the page ({kind})"))
    assert data_model._pyramidized_copy("/picked/ab/slide.tif", "/data/p", None, None) == found


# -- the missing files the page asks for --------------------------------------

def test_missing_files_say_which_copies_only_wait(website, monkeypatch, tmp_path):
    from plexora.server.models.project import Project
    from plexora import paths

    entry = {"sources": {
        "/derived/k/slide.pyramid.ome.tiff": {"name": "slide.pyramid.ome.tiff", "size": 9, "at": {}},
        "/picked/ab/slide.tif": {"name": "slide.tif", "size": 9, "at": {}},
    }}
    monkeypatch.setattr(Project, "root_for", staticmethod(lambda name: tmp_path))
    monkeypatch.setattr(relink, "read_config", lambda path: {"lung": entry}, raising=False)
    import plexora.server.models.project as project_module
    monkeypatch.setattr(project_module, "read_config", lambda path: {"lung": entry})
    monkeypatch.setattr(paths, "config_path", lambda root: tmp_path / "config.json")
    asked = []
    browser.set_asker(_asker({"/derived/k/slide.pyramid.ome.tiff": {"state": "waiting", "place": "“data”"}}, asked))
    missing = {item["recorded"]: item for item in relink.missing_here("lung")}
    assert missing["/derived/k/slide.pyramid.ome.tiff"]["waiting"] is True
    assert missing["/derived/k/slide.pyramid.ome.tiff"]["place"] == "“data”"
    assert "waiting" not in missing["/picked/ab/slide.tif"]
    assert asked == ["derived_index"]  # one question for the whole list


# -- the desktop app finds what the website built -----------------------------

def test_the_website_records_pyramids_it_wrote_into_the_store_and_a_masks_source(monkeypatch):
    seen = []

    def identify(path):
        seen.append(path)
        return {"name": Path(path).name, "size": 1}

    entry = {"segmentation": "/data/lung/derived/cells.outlines.pyramid.ome.tiff",
             "segmentationSource": "/picked/ab/cells.tif",
             "database": "/data/lung/lung.db"}
    out = sources.record(entry, "website", identify=identify, wanted=sources.website_file)
    known = out["sources"]
    assert "/data/lung/derived/cells.outlines.pyramid.ome.tiff" in known
    assert known["/data/lung/derived/cells.outlines.pyramid.ome.tiff"]["from"] == "/picked/ab/cells.tif"
    assert "/data/lung/lung.db" not in known  # the store's own files are not the user's
    assert "/picked/ab/cells.tif" in known


def test_a_file_the_websites_python_wrote_is_found_in_the_library(tmp_path, monkeypatch):
    documents = tmp_path / "Documents"
    monkeypatch.setattr(relink, "documents_dir", lambda: documents)
    monkeypatch.setattr(relink.Path, "home", staticmethod(lambda: tmp_path))
    written = documents / "Plexora" / "projects" / "lung" / "derived" / "cells.outlines.pyramid.ome.tiff"
    written.parent.mkdir(parents=True)
    written.write_bytes(b"p" * 4000)
    facts = sources.file_identity(written)
    entry = {"sources": {"/data/lung/derived/cells.outlines.pyramid.ome.tiff": {**facts, "at": {"website": "x"}}}}
    found = relink.resolve(entry, "host:LAB", walk_seconds=0, index=lambda name: [])
    assert found == {"/data/lung/derived/cells.outlines.pyramid.ome.tiff": str(written)}


def test_the_desktop_works_out_where_the_website_kept_its_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "app_key", lambda: "host:LAB")
    monkeypatch.setattr(relink, "documents_dir", lambda: tmp_path / "Documents")
    original = tmp_path / "data" / "slide.tif"
    original.parent.mkdir()
    original.write_bytes(b"s" * 1234)
    stat = original.stat()
    # The page reads the time in whole milliseconds.
    key = browser.derived_key("slide.tif", f"{stat.st_size}-{(stat.st_mtime_ns // 1_000_000) * 1_000_000}")
    copy = tmp_path / "Documents" / "Plexora" / "pyramids" / key / "slide.pyramid.ome.tiff"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(b"c")
    assert relink.website_copies(original, "slide.pyramid.ome.tiff") == [copy]
    assert relink.website_copies(original, "slide.pyramid.ome.tiff", "labels") == []
    monkeypatch.setattr(sources, "app_key", lambda: "website")
    assert relink.website_copies(original, "slide.pyramid.ome.tiff") == []


def test_a_desktop_resolver_adopts_the_websites_copy_and_never_writes_there(tmp_path, monkeypatch):
    from plexora.server.utils import image_pyramid

    original = tmp_path / "data" / "slide.tif"
    original.parent.mkdir()
    original.write_bytes(b"s")
    website_copy = tmp_path / "Documents" / "Plexora" / "pyramids" / "k" / "slide.pyramid.ome.tiff"
    monkeypatch.setattr(relink, "website_copies", lambda source, name, kind="image": [website_copy])
    monkeypatch.setattr(image_pyramid, "is_adoptable_image", lambda candidate, source: Path(candidate) == website_copy)
    where = image_pyramid.resolve_derived_image(original, tmp_path / "project")
    assert where.existing == website_copy
    assert website_copy.parent not in {where.target.parent}

"""The website's `.plexora` codec (client workers/bundleWorker.js) against
Python's own (server/models/bundles.py): a file the page writes is one the
desktop reads and imports, and the other way round -- and the seams the page
works through: the plan's room for the manifest, revisions for a manifest
finished from the page's sums, and an import whose large members the page
has already written into the store (`placements`, `apply(placed=...)`).

The codec's own checks (CRC-32 and SHA-256 against known values, the ZIP64
headers it writes) are in tests/js/bundle_codec_probe.mjs; this runs it and
pins its lines. Every test that needs Node is skipped without it.
"""

import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from plexora import paths
from plexora.server.models import bundles
from plexora.server.models.project import Project

from tests.test_bundles import _another_computer, _image, _register, _saved_state

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "js" / "bundle_codec_probe.mjs"
#: The transport (services/bundleWeb.js) driving the real worker, with Python
#: and the folders stood in for.
WEB_PROBE = REPO_ROOT / "tests" / "js" / "bundle_web_probe.mjs"

CHECKS = (
    "crc32 matches zlib's check values",
    "crc32 runs on across chunks and matches node's zlib on a megabyte",
    "sha256 matches the FIPS 180 vectors",
    "sha256 fed in odd chunks matches one digest of the whole",
    "a member is stored or deflated as Python chooses",
    "the manifest is the first member, stored, padded with spaces, its CRC patched",
    "a large member has ZIP64 sizes in its local header",
    "ZIP64 sizes and offsets in the central directory read back",
    "ZIP64 end records are found from the end record",
    "every member reads back with its CRC and sha256",
    "a damaged member is refused",
    "a member that inflates past its size is refused",
)


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


@pytest.fixture(scope="module")
def probe():
    return subprocess.run([_node(), str(PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=180)


def test_the_probe_passes(probe):
    assert probe.returncode == 0, f"{probe.stdout}\n{probe.stderr}"


@pytest.mark.parametrize("line", CHECKS)
def test_each_check_ran(probe, line):
    assert f"ok - {line}" in probe.stdout


def test_no_check_was_quietly_dropped(probe):
    assert f"{len(CHECKS)} checks passed" in probe.stdout


WEB_CHECKS = (
    "export: the Save dialog opens on the click, before anything is awaited",
    "export: the store is written back, then planned, then packed, the manifest first",
    "export: each member from where it is -- the folder, a pick, Python's work folder",
    "export: delivered where it was saved",
    "export: without a Save dialog, written into this site's storage and downloaded from there",
    "export: a cancelled one stops and takes its file away",
    "export: a manifest Python refuses takes the file away",
    "import: the picked file is mounted for Python, which previews it from the file",
    "import: Python's large members unpacked into the store, checked, and adopted",
    "import: a failed import takes back what the worker wrote",
    "import: a member that does not match is refused, nothing left, Python never asked",
    "find missing: the browser's own dialog, then Python told what was found",
    "the rest is the server transport's",
)


@pytest.fixture(scope="module")
def web_probe():
    return subprocess.run([_node(), str(WEB_PROBE)], capture_output=True, text=True, cwd=REPO_ROOT, timeout=300)


def test_the_transport_probe_passes(web_probe):
    assert web_probe.returncode == 0, f"{web_probe.stdout}\n{web_probe.stderr}"


@pytest.mark.parametrize("line", WEB_CHECKS)
def test_each_transport_check_ran(web_probe, line):
    assert f"ok - {line}" in web_probe.stdout


def test_no_transport_check_was_quietly_dropped(web_probe):
    assert f"{len(WEB_CHECKS)} checks passed" in web_probe.stdout


@pytest.fixture
def project(tmp_path):
    image = _image(tmp_path / "here" / "slide.ome.tif")
    _register("slide", image)
    return image


def _page_writes(plan: dict, out: Path, tmp_path: Path) -> dict:
    """The page's export: the worker streams the plan's members, Python
    finishes the manifest from its sums, the worker writes it first."""
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    worker = subprocess.Popen([_node(), str(PROBE), "--write", str(plan_file), str(out)], cwd=REPO_ROOT,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        packed = json.loads(worker.stdout.readline())
        finished = bundles.finish_manifest(plan["manifest"], packed["sums"], entries=plan["entries"],
                                           left_out=packed["left_out"])
        text = json.dumps(finished, indent=1, ensure_ascii=False).encode("utf-8")
        stdout, stderr = worker.communicate(text, timeout=120)
    finally:
        if worker.poll() is None:
            worker.kill()
    assert worker.returncode == 0, stderr.decode("utf-8", "replace")
    assert json.loads(stdout)["size"] == out.stat().st_size
    return finished


def test_python_reads_and_imports_what_the_page_writes(project, tmp_path, monkeypatch):
    before = _saved_state("slide")
    plan = bundles.plan_for_page(["slide"], work_dir=tmp_path / "work")
    assert plan["reserve"] > len(json.dumps(plan["manifest"]))
    out = tmp_path / "page.plexora"
    finished = _page_writes(plan, out, tmp_path)

    with zipfile.ZipFile(out) as bundle:
        assert bundle.testzip() is None
        infos = bundle.infolist()
        assert infos[0].filename == bundles.MANIFEST and infos[0].compress_type == zipfile.ZIP_STORED
        assert json.loads(bundle.read(bundles.MANIFEST)) == finished
        for record in bundles._records(finished):
            assert hashlib.sha256(bundle.read(record["path"])).hexdigest() == record["sha256"], record["path"]
        methods = {info.filename: info.compress_type for info in infos}
    pid = finished["projects"][0]["id"]
    assert methods[f"projects/{pid}/state/cells_edited.csv"] == zipfile.ZIP_DEFLATED

    # The revision is the one Python's own export gives the same project.
    desktop = bundles.export(["slide"], tmp_path / "desktop.plexora", record=False)["manifest"]
    assert finished["projects"][0]["revision"] == desktop["projects"][0]["revision"]
    assert bundles.ZipSource(out).manifest()["projects"][0]["revision"] == finished["projects"][0]["revision"]

    # And it imports on another computer with every saved row.
    _another_computer(tmp_path, monkeypatch)
    result = bundles.apply(out)
    assert result["projects"][0]["target"] == "slide"
    after = _saved_state("slide")
    assert after["channels"] == before["channels"] and after["gating"] == before["gating"]
    assert after["roi"].equals(before["roi"])


def test_what_the_page_could_not_read_is_left_out_of_the_manifest(project, tmp_path):
    plan = bundles.plan_for_page(["slide"], work_dir=tmp_path / "work")
    pid = plan["manifest"]["projects"][0]["id"]
    gone = f"projects/{pid}/state/plugins/gating/gates.json"
    for member in plan["members"]:
        if member["bundle_path"] == gone:
            Path(member["source"]).unlink()
    out = tmp_path / "page.plexora"
    finished = _page_writes(plan, out, tmp_path)
    assert gone not in {record["path"] for record in bundles._records(finished)}
    assert {"project": pid, "path": gone, "why": "gone before it was packed"} in finished["left_out"]
    with zipfile.ZipFile(out) as bundle:
        assert gone not in bundle.namelist() and bundle.testzip() is None


def test_the_page_reads_what_python_writes(project, tmp_path):
    out = tmp_path / "desktop.plexora"
    manifest = bundles.export(["slide"], out, include={"images": True})["manifest"]
    read = subprocess.run([_node(), str(PROBE), "--read", str(out)], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=120)
    assert read.returncode == 0, read.stderr
    members = json.loads(read.stdout)
    with zipfile.ZipFile(out) as bundle:
        assert set(members) == set(bundle.namelist())
        for info in bundle.infolist():
            assert members[info.filename]["size"] == info.file_size
            assert members[info.filename]["crc"] == info.CRC
            assert members[info.filename]["method"] == info.compress_type
    for record in bundles._records(manifest):
        assert members[record["path"]]["sha256"] == record["sha256"], record["path"]
    # The included image is stored (it is a TIFF that does not deflate), the
    # table deflated.
    media = manifest["media"][0]["path"]
    assert members[media]["sha256"] == hashlib.sha256(project.read_bytes()).hexdigest()


# -- an import whose large members the page wrote itself ---------------------


def _place(out: Path, placements: list) -> dict:
    """What the page's worker does: each member written where Python said."""
    with zipfile.ZipFile(out) as bundle:
        for item in placements:
            target = Path(item["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(item["path"]) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink)
    return {item["path"]: item["target"] for item in placements}


def test_placed_members_are_taken_where_they_are_and_never_unpacked_again(project, tmp_path, monkeypatch):
    before = _saved_state("slide")
    out = tmp_path / "slide.plexora"
    manifest = bundles.export(["slide"], out, include={"images": True})["manifest"]
    pid = manifest["projects"][0]["id"]
    _another_computer(tmp_path, monkeypatch)
    project.unlink()

    places = bundles.placements(out, None, None, at_least=0)
    paths_placed = {item["path"] for item in places}
    # Never the entry or the database (its rows are renamed here), always the
    # image and the table, each where the import puts it.
    assert f"projects/{pid}/entry.json" not in paths_placed
    assert f"projects/{pid}/state/slide.db" not in paths_placed
    assert manifest["media"][0]["path"] in paths_placed
    assert f"projects/{pid}/state/cells_edited.csv" in paths_placed
    root = paths.new_projects_root("slide")
    assert all(Path(item["target"]).is_relative_to(root / "slide") for item in places)
    assert all(item["sha256"] for item in places)
    # A threshold leaves the small ones to Python.
    assert {item["path"] for item in bundles.placements(out, None, None, at_least=10_000)} < paths_placed

    placed = _place(out, places)
    unpacked = []
    extract = bundles._extract
    monkeypatch.setattr(bundles, "_extract", lambda source, record, target, meter: (
        unpacked.append(record["path"]), extract(source, record, target, meter))[1])
    result = bundles.apply(out, placed=placed)["projects"][0]
    # The folder the page filled is not taken for somebody else's.
    assert result["target"] == "slide" and result["refs"]["included"] == 1
    assert not set(unpacked) & paths_placed
    assert f"projects/{pid}/state/slide.db" in unpacked
    for where in placed.values():
        assert Path(where).is_file()
    image = Path(Project.load("slide").image.src)
    assert image.is_file() and image.read_bytes() == Path(placed[manifest["media"][0]["path"]]).read_bytes()
    after = _saved_state("slide")
    assert after["channels"] == before["channels"] and after["gating"] == before["gating"]


def test_a_failed_import_takes_back_what_the_page_placed(project, tmp_path, monkeypatch):
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out, include={"images": True})
    _another_computer(tmp_path, monkeypatch)
    placed = _place(out, bundles.placements(out, None, None))
    folder = Path(next(iter(placed.values()))).parents[0]
    while folder.parent != paths.new_projects_root("slide"):
        folder = folder.parent

    def broken(*args, **kwargs):
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(bundles, "_merge_datasets", broken)
    with pytest.raises(RuntimeError):
        bundles.apply(out, placed=placed)
    assert not any(Path(where).exists() for where in placed.values())
    assert not folder.exists()
    assert "slide" not in Project.load_all()


def test_an_import_refused_before_it_starts_takes_back_what_was_placed(project, tmp_path, monkeypatch):
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out)
    _another_computer(tmp_path, monkeypatch)
    placed = _place(out, bundles.placements(out, None, None))
    assert placed
    monkeypatch.setattr(bundles, "_free_bytes", lambda root: 1)
    with pytest.raises(bundles.BundleError):
        bundles.apply(out, placed=placed)
    assert not any(Path(where).exists() for where in placed.values())
    # The folders made for them go too; the root stays.
    assert not (paths.new_projects_root("slide") / "slide").exists()
    assert paths.new_projects_root("slide").is_dir()


def test_an_update_or_a_skip_is_left_to_python(project, tmp_path, monkeypatch):
    out = tmp_path / "slide.plexora"
    pid = bundles.export(["slide"], out)["manifest"]["projects"][0]["id"]
    # Here already: the default is to skip it, and nothing is placed; an
    # update's folder is moved aside first, so its members are unpacked here.
    assert bundles.placements(out, None, None) == []
    assert bundles.placements(out, {pid: "replace"}, None) == []
    assert bundles.placements(out, {pid: "keep_both"}, None)


def test_the_routes_carry_placements_and_what_was_placed(project, tmp_path, monkeypatch):
    import time

    import plexora

    client = plexora.app.test_client()
    out = tmp_path / "slide.plexora"
    bundles.export(["slide"], out, include={"images": True})
    _another_computer(tmp_path, monkeypatch)
    preview = client.post("/projects/import/preview", json={"path": str(out), "place_from": 0}).get_json()
    assert preview["projects"][0]["status"] == "new" and preview["placements"]
    placed = _place(out, preview["placements"])
    started = client.post("/projects/import", json={"path": str(out), "placed": placed})
    assert started.status_code == 202
    job = started.get_json()["job"]
    deadline = time.monotonic() + 60
    status = client.get(f"/projects/import/{job}").get_json()
    while status["status"] == "pending" and time.monotonic() < deadline:
        time.sleep(0.05)
        status = client.get(f"/projects/import/{job}").get_json()
    assert status["status"] == "ready", status
    assert status["result"]["projects"][0]["target"] == "slide"
    assert client.post("/projects/import", json={"path": str(out), "placed": {"a": 1}}).status_code == 400


def test_the_manifest_route_records_the_revision_and_clears_the_work_folder(project):
    import tempfile

    import plexora

    client = plexora.app.test_client()
    plan = client.post("/projects/export/plan", json={"ids": ["slide"]}).get_json()
    work = Path(plan["work_dir"])
    assert work.is_dir() and work.parent == Path(tempfile.gettempdir())
    sums = {}
    for member in plan["members"]:
        data = Path(member["source"]).read_bytes()
        sums[member["bundle_path"]] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    finished = client.post("/projects/export/manifest", json={
        "manifest": plan["manifest"], "sums": sums, "entries": plan["entries"], "stamps": plan["stamps"],
        "work_dir": plan["work_dir"]}).get_json()
    revision = finished["projects"][0]["revision"]
    assert revision and len(revision) == 64
    assert not work.exists()
    entry = Project.load_all()["slide"]
    assert entry["bundleRevision"]["id"] == revision
    # Exported again unchanged: the same revision, and the same parent.
    again = bundles.finish_manifest(plan["manifest"], sums, entries=plan["entries"])
    assert again["projects"][0]["revision"] == revision
    assert again["projects"][0]["parent"] == entry["bundleRevision"]["parent"]

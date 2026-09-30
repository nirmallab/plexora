"""Every file added on a sample's card stays that sample's.

The reported failure: two samples, each with its own image and segmentation
mask, each mask added through its own card -- and after import both masks
belonged to one sample. An mcmicro run names every sample's mask
`cellRing.ome.tif`, and the dialog filed "which card is this on" and "what was
it added as" under the FILENAME, so the second card's answer overwrote the
first's. Every per-pick answer is now filed under the whole pick.

The same rule is held here for every modality a card can add -- mask, data,
layer -- and for the two ways a pick's spelling changes between the screen and
the server (forward slashes on Windows, and a pick removed beside another of
the same name).

The second half is the lifecycle of what reading a pick shares on a data
node: a mask removed before import, or a dialog closed, must not leave the
file registered there -- and a registration that WAS left behind must not
refuse the same file when the viewer asks for it later.
"""

from pathlib import Path

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import import_proposal, layer_jobs
from plexora.server.models.import_proposal import inspect_paths
from plexora.server.models.project import Project

from tests.helpers import use_data_root


def _image(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(path, np.random.default_rng(0).integers(
        0, 4000, (2, 128, 128)).astype(np.uint16))
    return path


def _mask(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(path, (np.arange(128 * 128).reshape(128, 128) % 50)
                     .astype(np.uint32))
    return path


def _table(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("CellID,X_centroid,Y_centroid,CD3\n1,10,10,5\n",
                    encoding="utf-8")
    return path


def _run(tmp_path, name):
    """One mcmicro sample: its image named after it, its mask not."""
    root = tmp_path / name
    image = _image(root / "registration" / f"{name}.ome.tif")
    mask = _mask(root / "segmentation" / "unmicst" / "cellRing.ome.tif")
    return image, mask


def _on_card(answers, pick, key, role):
    """What the card's own "+ Add …" files: under the WHOLE pick."""
    answers[f"sample-for:{pick}"] = key
    answers[f"added-as:{pick}"] = role


def _owner_of(proposal, src):
    return [sample.name for sample in proposal.samples
            if any(str(layer.src) == str(src) for layer in sample.layers)]


# -- the reported bug ------------------------------------------------------

def test_two_samples_same_named_masks_each_stay_on_their_own_card(tmp_path):
    image_a, mask_a = _run(tmp_path, "lsp11641")
    image_b, mask_b = _run(tmp_path, "mel0293")
    first = inspect_paths([str(image_a), str(image_b)])
    keys = {sample.name: sample.key for sample in first.samples}

    answers = {}
    _on_card(answers, str(mask_a), keys["lsp11641"], "mask")
    _on_card(answers, str(mask_b), keys["mel0293"], "mask")
    proposal = inspect_paths(
        [str(image_a), str(image_b), str(mask_a), str(mask_b)], answers=answers)

    assert len(proposal.samples) == 2
    assert _owner_of(proposal, mask_a) == ["lsp11641"]
    assert _owner_of(proposal, mask_b) == ["mel0293"]
    for sample in proposal.samples:
        masks = [layer for layer in sample.layers if layer.role == "mask"]
        assert len(masks) == 1, (sample.name, [l.src for l in sample.layers])


def test_the_screens_spelling_of_a_pick_finds_its_answer(tmp_path):
    """The dialog files an answer under the string IT sent, which the route
    tidies on the way in -- on Windows, `C:/…` arrives as `C:\\…`. Compared
    tidied, or the lookup falls back to the filename and collides again."""
    image_a, mask_a = _run(tmp_path, "lsp11641")
    image_b, mask_b = _run(tmp_path, "mel0293")
    keys = {s.name: s.key for s in inspect_paths([str(image_a),
                                                  str(image_b)]).samples}
    answers = {}
    _on_card(answers, mask_a.as_posix(), keys["lsp11641"], "mask")
    _on_card(answers, mask_b.as_posix(), keys["mel0293"], "mask")

    proposal = inspect_paths(
        [str(image_a), str(image_b), str(mask_a), str(mask_b)], answers=answers)

    assert _owner_of(proposal, mask_a) == ["lsp11641"]
    assert _owner_of(proposal, mask_b) == ["mel0293"]


def test_removing_one_samples_mask_leaves_the_other_as_it_was(tmp_path):
    """The step that turned the bug into a stale node resource: removing
    sample 1's mask deleted the ONE answer both masks shared, and sample 2's
    mask was read again as an image."""
    image_a, mask_a = _run(tmp_path, "lsp11641")
    image_b, mask_b = _run(tmp_path, "mel0293")
    keys = {s.name: s.key for s in inspect_paths([str(image_a),
                                                  str(image_b)]).samples}
    answers = {}
    _on_card(answers, str(mask_a), keys["lsp11641"], "mask")
    _on_card(answers, str(mask_b), keys["mel0293"], "mask")
    for key in (f"sample-for:{mask_a}", f"added-as:{mask_a}"):
        answers.pop(key)

    proposal = inspect_paths([str(image_a), str(image_b), str(mask_b)],
                             answers=answers)

    held = [layer for sample in proposal.samples for layer in sample.layers
            if str(layer.src) == str(mask_b)]
    assert [layer.role for layer in held] == ["mask"]
    assert _owner_of(proposal, mask_b) == ["mel0293"]


@pytest.mark.parametrize("role, write, expected", [
    ("table", _table, "table"),
    ("layer", _image, "layer"),
])
def test_every_modality_a_card_adds_stays_on_that_card(tmp_path, role, write,
                                                       expected):
    """Data and layers, by the same rule as masks: two samples' `cells.csv`
    or `he.ome.tif` from their own folders, added on their own cards."""
    image_a, _ = _run(tmp_path, "lsp11641")
    image_b, _ = _run(tmp_path, "mel0293")
    suffix = ".csv" if role == "table" else ".ome.tif"
    extra_a = write(tmp_path / "lsp11641" / "extra" / f"shared{suffix}")
    extra_b = write(tmp_path / "mel0293" / "extra" / f"shared{suffix}")
    keys = {s.name: s.key for s in inspect_paths([str(image_a),
                                                  str(image_b)]).samples}
    answers = {}
    _on_card(answers, str(extra_a), keys["lsp11641"], role)
    _on_card(answers, str(extra_b), keys["mel0293"], role)

    proposal = inspect_paths(
        [str(image_a), str(image_b), str(extra_a), str(extra_b)],
        answers=answers)

    assert _owner_of(proposal, extra_a) == ["lsp11641"]
    assert _owner_of(proposal, extra_b) == ["mel0293"]
    roles = {str(layer.src): layer.role
             for sample in proposal.samples for layer in sample.layers}
    assert roles[str(extra_a)] == roles[str(extra_b)] == expected


def test_two_folders_same_named_image_are_two_samples_with_two_keys(tmp_path):
    """Two runs' `image.ome.tif`. By stem alone they were one group, and
    every same-stemmed row landed in both samples; the folder tells them
    apart, and each keeps what sits beside it."""
    image_a = _image(tmp_path / "a" / "image.ome.tif")
    image_b = _image(tmp_path / "b" / "image.ome.tif")
    mask_a = _mask(tmp_path / "a" / "image_mask.tif")
    mask_b = _mask(tmp_path / "b" / "image_mask.tif")

    proposal = inspect_paths([str(image_a), str(image_b),
                              str(mask_a), str(mask_b)])

    assert len(proposal.samples) == 2
    assert len({sample.key for sample in proposal.samples}) == 2
    for image, mask in ((image_a, mask_a), (image_b, mask_b)):
        owner = [s for s in proposal.samples
                 if any(str(l.src) == str(image) for l in s.layers)]
        assert len(owner) == 1
        assert {str(l.src) for l in owner[0].layers} == {str(image), str(mask)}


def test_a_slide_and_its_he_in_one_folder_are_still_one_sample(tmp_path):
    """Same stem in the SAME folder is one sample, as it always was: the
    pyramid and its flat export are one slide drawn twice."""
    _image(tmp_path / "LSP11641.ome.tif")
    _image(tmp_path / "LSP11641.tif")
    proposal = inspect_paths([str(tmp_path / "LSP11641.ome.tif"),
                              str(tmp_path / "LSP11641.tif")])
    assert len(proposal.samples) == 1
    assert len(proposal.samples[0].layers) == 2


# -- end to end: registered where it was added -----------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    layer_jobs.forget()
    yield plexora.app.test_client()
    layer_jobs.forget()


def test_each_samples_mask_is_registered_on_that_sample(client, tmp_path):
    image_a, mask_a = _run(tmp_path / "source", "lsp11641")
    image_b, mask_b = _run(tmp_path / "source", "mel0293")
    paths = [str(image_a), str(image_b), str(mask_a), str(mask_b)]
    keys = {s["name"]: s["key"] for s in client.post(
        "/import/inspect", json={"paths": paths[:2]}).get_json()["samples"]}
    answers = {}
    _on_card(answers, mask_a.as_posix(), keys["lsp11641"], "mask")
    _on_card(answers, mask_b.as_posix(), keys["mel0293"], "mask")

    proposal = client.post("/import/inspect", json={
        "paths": paths, "answers": answers}).get_json()
    for index, sample in enumerate(proposal["samples"]):
        response = client.post("/import/sample", json={
            "paths": paths, "answers": answers, "index": index,
            "key": sample["key"]})
        assert response.status_code == 200, response.get_json()

    for name, mask in (("lsp11641", mask_a), ("mel0293", mask_b)):
        segmentation = Project.load(name).segmentation
        assert segmentation.requested is True
        assert Path(segmentation.source).resolve() == mask.resolve(), name


# -- what reading a pick shares on a node ----------------------------------

class _FakeNode:
    """A node's registry, as `plexora.nodes` sees it: `(kind, id, path)`."""

    def __init__(self):
        self.served = {}

    def node_resources(self, node):
        return [{"id": rid, "kind": kind} for rid, (kind, _) in
                self.served.items()]

    def share_path(self, node, kind, path):
        from plexora.nodes import resource_id_for
        from plexora.server.providers.base import ResourceError

        rid = resource_id_for(path)
        if rid in self.served and self.served[rid] != (kind, path):
            raise ResourceError(f"this node already serves a different "
                                f"resource called {rid!r}")
        self.served[rid] = (kind, path)
        return {"id": rid, "kind": kind, "locator": f"node://{node}/{rid}"}

    def unshare_path(self, node, rid):
        self.served.pop(rid, None)
        return {}


@pytest.fixture
def node(monkeypatch):
    from plexora import nodes

    fake = _FakeNode()
    for name in ("node_resources", "share_path", "unshare_path"):
        monkeypatch.setattr(nodes, name, getattr(fake, name))
    monkeypatch.setattr(import_proposal, "_SHARED_BY_INSPECTION", set())
    return fake


def test_a_stale_registration_under_another_kind_is_replaced(node):
    """The error from the viewer: `data node 'hms-o2' disagrees about the
    request: this node already serves a different resource called
    'cellring-ome-07f9fea0'`. Left by an abandoned import as an image;
    asked for as a mask. Nothing reads it, so it is re-served as asked."""
    from plexora import nodes

    path = "/n/scratch/run/segmentation/cellRing.ome.tif"
    node.share_path("hms-o2", "image", path)

    described = nodes.share_path_replacing("hms-o2", "segmentation", path,
                                           owner_of=lambda *_: None)

    assert described["kind"] == "segmentation"
    assert node.served[nodes.resource_id_for(path)][0] == "segmentation"


def test_a_registration_a_project_reads_is_not_pulled_from_under_it(node):
    from plexora import nodes
    from plexora.server.providers.base import ResourceError

    path = "/n/scratch/run/registration/slide.ome.tif"
    node.share_path("hms-o2", "image", path)

    with pytest.raises(ResourceError, match="slide"):
        nodes.share_path_replacing("hms-o2", "segmentation", path,
                                   owner_of=lambda *_: "slide")
    assert node.served[nodes.resource_id_for(path)][0] == "image"


def test_releasing_a_pick_unshares_only_what_inspection_shared(node,
                                                                monkeypatch):
    from plexora import nodes

    shared = "/n/scratch/run/segmentation/cellRing.ome.tif"
    already = "/n/scratch/run/registration/slide.ome.tif"
    bound = "/n/scratch/run/registration/other.ome.tif"
    node.share_path("hms-o2", "image", already)       # served before
    for path in (shared, bound):
        node.share_path("hms-o2", "segmentation", path)
        import_proposal._SHARED_BY_INSPECTION.add(
            ("hms-o2", nodes.resource_id_for(path)))
    monkeypatch.setattr(
        import_proposal, "_bound_project",
        lambda n, rid: "other" if rid == nodes.resource_id_for(bound) else None)

    released = import_proposal.release_picks(
        [f"node://hms-o2/{p}" for p in (shared, already, bound)]
        + ["C:/local/file.tif", ""])

    assert released == [f"node://hms-o2/{nodes.resource_id_for(shared)}"]
    assert nodes.resource_id_for(shared) not in node.served
    assert nodes.resource_id_for(already) in node.served
    assert nodes.resource_id_for(bound) in node.served
    assert not import_proposal._SHARED_BY_INSPECTION


def test_the_release_route_answers_what_it_released(client, node):
    response = client.post("/import/release", json={
        "paths": ["node://hms-o2//n/scratch/never-shared.tif"]})
    assert response.status_code == 200
    assert response.get_json() == {"released": []}


# -- run folders and the questions asked inside one pick --------------------

def test_two_run_folders_are_two_samples(tmp_path):
    """Two Xenium runs, each added as a sample, came back as ONE card named
    after the first -- one run's cell table and transcripts, the other's rows
    gone without a word."""
    from tests.test_import_proposal import _xenium_run

    run_a = _xenium_run(tmp_path / "run_a")
    run_b = _xenium_run(tmp_path / "run_b")

    proposal = inspect_paths([str(run_a), str(run_b)])

    assert sorted(sample.name for sample in proposal.samples) == ["run_a",
                                                                  "run_b"]
    assert len({sample.key for sample in proposal.samples}) == 2
    for sample in proposal.samples:
        roots = {layer.bundle["root"] for layer in sample.layers if layer.bundle}
        assert roots == {sample.bundles[0]["root"]}, sample.name


def test_a_question_inside_one_store_is_answered_for_that_store_only(tmp_path):
    """"Which image is this sample drawn in?" was asked with the id
    `reference` for every store, so answering it on sample 1's card answered
    it on sample 2's. Asked per pick now; a bare `reference` still applies to
    every pick, for scripts."""
    from tests.test_single_image_import import write_spatialdata_like

    one = write_spatialdata_like(tmp_path / "one.zarr",
                                 elements=("dapi", "morphology"),
                                 shape=(1, 64, 64), levels=1)
    two = write_spatialdata_like(tmp_path / "two.zarr",
                                 elements=("dapi", "morphology"),
                                 shape=(1, 64, 64), levels=1)
    first = inspect_paths([str(one), str(two)])
    asked = {q.id: q for sample in first.samples for q in sample.questions
             if q.id.startswith("reference@")}
    assert set(asked) == {f"reference@{one}", f"reference@{two}"}
    other = next(option["value"] for option in asked[f"reference@{one}"].options
                 if option["value"] != asked[f"reference@{one}"].default)

    answered = inspect_paths([str(one), str(two)],
                             answers={f"reference@{one}": other})

    references = {Path(sample.bundles[0]["root"]).name if sample.bundles
                  else sample.name:
                  next(l.id for l in sample.layers if l.reference)
                  for sample in answered.samples}
    assert len(references) == 2
    assert len(set(references.values())) == 2, references

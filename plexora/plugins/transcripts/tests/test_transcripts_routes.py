"""The transcripts plugin's HTTP surface.

Three routes, and the interesting assertions are about the line between this
plugin and core rather than about any one of them:

**The gene vocabulary is served from the derived manifest, never from /config.**
`/config` reaches the client on every viewer boot for every project the user has.
A 300-gene list inlined there would be downloaded on every page load of every
project, including the ones with no transcripts at all.

**Density is not served here.** It is a uint16 raster on core's layer route,
because it is a channel like any other -- which is what makes the low-zoom view
cost no new client rendering code and survive into Figure Builder's export.

**The wire format is binary.** JSON for two million points is 60 MB of ASCII and
a second of parsing, and the records are 10 bytes each, packed.
"""

import gzip

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import transcript_tiles as tt
from plexora.server.models.project import ImageSpec, LayerSpec, Project

from tests.helpers import use_data_root


GENES = ["EPCAM", "CD3E", "PTPRC"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    use_data_root(monkeypatch, tmp_path)
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    image = tmp_path / "morphology.ome.tif"
    tifffile.imwrite(image, np.zeros((1, 256, 256), dtype=np.uint16))

    Project(
        name="xen",
        image=ImageSpec(src=str(image), width=256, height=256, max_level=1,
                        tile_width=256, tile_height=256,
                        channels=({"name": "c0", "fullname": "DAPI", "src": "/t/0"},)),
    ).with_layer(LayerSpec(id="tx", kind="points", label="Transcripts")).save()

    rng = np.random.default_rng(3)
    n = 600
    source = tmp_path / "transcripts.parquet"
    source.write_bytes(b"stand-in; only its size and mtime are read")
    tt.build("xen", "tx",
             genes=GENES,
             gene_index=rng.integers(0, len(GENES), size=n).astype(np.uint16),
             x=rng.uniform(0, 256, size=n).astype(np.float32),
             y=rng.uniform(0, 256, size=n).astype(np.float32),
             expected=tt.expected_manifest(source, width=256, height=256,
                                           tile_size=128, layer_id="tx"))
    return "xen"


@pytest.fixture
def client(project):
    return plexora.app.test_client()


def _points(response):
    raw = response.get_data()
    if response.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return np.frombuffer(raw, dtype=tt.POINT_DTYPE)


# -- the manifest -----------------------------------------------------------

def test_the_manifest_carries_the_gene_vocabulary(client, project):
    body = client.get("/plugins/transcripts/manifest?datasource=xen&layer=tx").get_json()

    assert body["status"] == "ready"
    assert body["genes"] == GENES
    assert body["point_count"] == 600


def test_the_manifest_carries_the_per_tile_counts(client, project):
    """What the client's LOD reads. Without them the viewer would have to fetch
    a tile to find out how many points are in it -- which is fetching the thing
    it is deciding whether to fetch."""
    body = client.get("/plugins/transcripts/manifest?datasource=xen&layer=tx").get_json()

    counts = np.array(body["tile_counts"])
    assert counts.shape == (2, 2)
    assert counts.sum() == 600


def test_the_gene_list_is_not_in_the_config_route(client, project):
    """The rule, not an implementation detail. /config reaches the client on
    every viewer boot for every project; a vocabulary inlined there would be
    downloaded for projects that have no transcripts at all."""
    config = client.get("/config").get_json()
    entry = config["xen"]

    assert "EPCAM" not in str(entry)
    # The LAYER is there -- that is what the viewer needs to know exists.
    assert any(layer["id"] == "tx" for layer in entry["layers"])


def test_a_layer_with_no_cache_reports_missing(client, project):
    body = client.get("/plugins/transcripts/manifest?datasource=xen&layer=nope").get_json()

    assert body["status"] == "missing"
    assert body["genes"] == []


# -- points -----------------------------------------------------------------

def test_points_come_back_as_packed_binary(client, project):
    response = client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx"
        "&minX=0&minY=0&maxX=256&maxY=256")

    assert response.status_code == 200
    assert response.mimetype == "application/octet-stream"
    assert int(response.headers["X-Transcript-Record-Count"]) == 600
    assert len(_points(response)) == 600


def test_points_can_be_restricted_to_some_genes(client, project):
    everything = _points(client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx"
        "&minX=0&minY=0&maxX=256&maxY=256"))
    one = _points(client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx&genes=CD3E"
        "&minX=0&minY=0&maxX=256&maxY=256"))

    assert 0 < len(one) < len(everything)
    assert set(one["gene"].tolist()) == {GENES.index("CD3E")}


def test_a_gene_the_panel_does_not_have_draws_nothing(client, project):
    """Nothing, not everything. The user asked for specific genes; drawing the
    whole panel instead is the opposite of what they asked for."""
    response = client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx&genes=NOT_A_GENE"
        "&minX=0&minY=0&maxX=256&maxY=256")

    assert int(response.headers["X-Transcript-Record-Count"]) == 0


def test_a_known_gene_beside_an_unknown_one_still_draws(client, project):
    """A saved view naming a gene a re-imported run no longer carries should
    draw the rest."""
    response = client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx&genes=CD3E,NOT_A_GENE"
        "&minX=0&minY=0&maxX=256&maxY=256")

    assert int(response.headers["X-Transcript-Record-Count"]) > 0


def test_a_viewport_restricts_what_comes_back(client, project):
    everything = int(client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx"
        "&minX=0&minY=0&maxX=256&maxY=256").headers["X-Transcript-Record-Count"])
    corner = int(client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx"
        "&minX=0&minY=0&maxX=100&maxY=100").headers["X-Transcript-Record-Count"])

    assert corner < everything


def test_a_truncated_response_says_so(client, project):
    """A sample drawn as though it were everything reads as "this gene is not
    expressed here", which is the one wrong conclusion to invite."""
    response = client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx&max=50"
        "&minX=0&minY=0&maxX=256&maxY=256")

    assert response.headers["X-Transcript-Truncated"] == "1"
    assert int(response.headers["X-Transcript-Record-Count"]) <= 50


def test_an_untruncated_response_says_that_too(client, project):
    response = client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx"
        "&minX=0&minY=0&maxX=256&maxY=256")

    assert response.headers["X-Transcript-Truncated"] == "0"


def test_a_layer_with_no_cache_serves_no_points(client, project):
    response = client.get(
        "/plugins/transcripts/points?datasource=xen&layer=nope"
        "&minX=0&minY=0&maxX=10&maxY=10")

    assert response.status_code == 200
    assert int(response.headers["X-Transcript-Record-Count"]) == 0


# -- build ------------------------------------------------------------------

def test_building_needs_a_datasource_and_a_source(client, project):
    assert client.post("/plugins/transcripts/build", json={}).status_code == 400


def test_building_for_an_unknown_project_is_a_404(client, project):
    response = client.post("/plugins/transcripts/build",
                           json={"datasource": "ghost", "source": "/x.parquet"})

    assert response.status_code == 404


def test_status_reports_a_layer_that_is_already_built(client, project):
    body = client.get("/plugins/transcripts/status?datasource=xen&layer=tx").get_json()

    assert body["status"] == "ready"


def test_status_reports_a_layer_that_was_never_built(client, project):
    body = client.get("/plugins/transcripts/status?datasource=xen&layer=nope").get_json()

    assert body["status"] == "missing"


# -- one job vocabulary -----------------------------------------------------
#
# The build used to run on a thread this plugin started, reporting
# `{status, stage, done, total}` into a dict it kept for itself -- which no
# other surface could read and a restart lost. It goes through core's
# `layer_jobs` now, so the importer and this panel see ONE record whoever
# started the build, and these pin the words that record uses.

def test_the_panel_and_the_route_agree_about_the_words(project):
    """`pending`, `ready`, `failed`. A panel reading `running` would sit on
    "Preparing…" with an empty label forever -- correct-looking and stuck."""
    from pathlib import Path

    controller = (Path(plexora.__file__).parent / "plugins" / "transcripts"
                  / "static" / "transcriptsSidebarController.js"
                  ).read_text(encoding="utf-8")
    assert 'state.status === "pending"' in controller
    assert 'state.status === "failed"' in controller
    assert '"running"' not in controller


def test_the_build_takes_its_source_from_the_layer(client, project):
    """Not from the request. A transcript file the project has never heard of
    is not something to build tiles for under this project's name -- and the
    layer is where the importer already wrote the path."""
    response = client.post("/plugins/transcripts/build",
                           json={"datasource": "xen", "layer": "nope"})

    assert response.status_code == 404
    assert "nope" in response.get_json()["error"]


def test_a_build_reports_through_the_shared_registry(client, project,
                                                     monkeypatch):
    """Which is what lets somebody who opens this panel mid-import see the
    import's progress rather than starting a second build over one cache."""
    from dataclasses import replace

    from plexora.server.models import layer_jobs

    # The fixture's layer has no source -- it was built from arrays. Giving it
    # one is what the importer does, and is what makes a rebuild possible at
    # all: the path is on the LAYER now, not in the request.
    Project.mutate("xen", lambda p: p.with_layer(
        replace(p.layer("tx"), src="/data/transcripts.parquet")))

    layer_jobs.forget()
    started = []
    monkeypatch.setattr(layer_jobs, "start",
                        lambda *a, **k: started.append(a[:2]) or {"status": "pending"})

    response = client.post("/plugins/transcripts/build",
                           json={"datasource": "xen", "layer": "tx"})

    assert response.status_code == 202
    assert started == [("xen", "tx")]


# -- one tile at a time -----------------------------------------------------

def test_a_tile_can_be_asked_for_by_its_address(client, project):
    """The mode the viewer uses, and the reason panning is cheap: a tile is a
    fixed, addressable unit, so the client keeps the ones it has and asks only
    for the ones that came into view."""
    whole = int(client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx"
        "&minX=0&minY=0&maxX=256&maxY=256").headers["X-Transcript-Record-Count"])
    tiles = [int(client.get(
        f"/plugins/transcripts/points?datasource=xen&layer=tx&tile={x}_{y}"
    ).headers["X-Transcript-Record-Count"]) for x in (0, 1) for y in (0, 1)]

    assert sum(tiles) == whole
    assert all(count > 0 for count in tiles)


def test_a_tile_is_cacheable_and_a_rectangle_is_not(client, project):
    """A tile's contents cannot change while the cache does not, so the
    browser is told it can keep it. A rectangle is a different rectangle every
    pan and caching one would only fill the disk."""
    tile = client.get("/plugins/transcripts/points?datasource=xen&layer=tx&tile=0_0")
    rect = client.get("/plugins/transcripts/points?datasource=xen&layer=tx"
                      "&minX=0&minY=0&maxX=128&maxY=128")

    assert "max-age=31536000" in tile.headers["Cache-Control"]
    assert tile.headers.get("ETag")
    assert "max-age=0" in rect.headers["Cache-Control"]


def test_two_gene_selections_are_different_tiles(client, project):
    """The ETag has to carry the selection, or a browser would serve one
    gene's points for another's."""
    one = client.get("/plugins/transcripts/points?datasource=xen&layer=tx"
                     "&tile=0_0&genes=CD3E").headers["ETag"]
    two = client.get("/plugins/transcripts/points?datasource=xen&layer=tx"
                     "&tile=0_0&genes=EPCAM").headers["ETag"]

    assert one != two


def test_a_malformed_tile_address_draws_nothing(client, project):
    response = client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx&tile=north")

    assert response.status_code == 200
    assert int(response.headers["X-Transcript-Record-Count"]) == 0


def test_the_record_size_is_stated_on_every_response(client, project):
    """The client decodes a packed binary buffer at a fixed stride, and a
    stride it guessed wrong turns every point into noise -- so the server
    says what it wrote."""
    response = client.get(
        "/plugins/transcripts/points?datasource=xen&layer=tx&tile=0_0")

    assert int(response.headers["X-Transcript-Record-Size"]) == tt.POINT_DTYPE.itemsize


# -- a cache from an older build --------------------------------------------

def test_a_cache_at_the_old_stride_reports_stale_rather_than_ready(
        client, project, tmp_path):
    """`stale`, not `ready`. A 10-byte cache decoded at 11 bytes comes back as
    noise -- every point somewhere else on the slide, plausibly distributed,
    with nothing on screen to say so."""
    import json

    path = tt.manifest_path("xen", "tx")
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["version"] = 1
    stored["record_dtype"] = "gene:uint16,x:float32,y:float32"
    path.write_text(json.dumps(stored), encoding="utf-8")

    body = client.get(
        "/plugins/transcripts/manifest?datasource=xen&layer=tx").get_json()

    assert body["status"] == "stale"
    assert body["genes"] == []


# -- the panel's own state --------------------------------------------------

def test_the_panel_state_round_trips(client, project):
    """Per PROJECT, not per session. A gene selection is an analysis decision,
    and somebody who set one up and came back the next day to an empty panel
    would reasonably conclude the tool had lost their work."""
    assert client.get("/plugins/transcripts/state?datasource=xen").get_json() == {}

    saved = {"selected": ["CD3E", "EPCAM"],
             "colors": {"CD3E": "#ff0000"},
             "groups": [{"name": "T cells", "genes": ["CD3E"]}],
             "viewAs": "points", "minQ": 20}
    client.post("/plugins/transcripts/state",
                json={**saved, "datasource": "xen"})

    assert client.get("/plugins/transcripts/state?datasource=xen").get_json() == saved


def test_saving_state_without_a_project_is_refused(client, project):
    assert client.post("/plugins/transcripts/state", json={}).status_code == 400


# -- what the density controls need to speak in real units ------------------

def test_the_manifest_says_how_big_a_pixel_is(client, project, tmp_path):
    """A bin size is a PHYSICAL size: "40 by 40 microns" is a sentence about
    tissue and "188 pixels" is one about this scan. The panel asks in microns
    and converts, and this is the only number it can convert with."""
    import dataclasses

    record = Project.load("xen")
    record.patch(image=dataclasses.replace(
        record.image,
        pixel_size={"value": 0.2125, "unit": "µm", "source": "metadata"})).save()

    body = client.get("/plugins/transcripts/manifest?datasource=xen&layer=tx").get_json()

    assert body["pixel_size"] == pytest.approx(0.2125)
    # Sent rather than repeated in the client: the legend under the ramp
    # claims a number of molecules per bin, and a legend that disagreed with
    # the picture would be worse than no legend.
    assert body["density_stretch"] == 8


def test_a_project_that_never_recorded_a_pixel_size_says_so(client, project):
    """Null rather than a guess. The panel relabels its slider in pixels,
    which is honest -- converting with a made-up scale would draw bins
    labelled with a size they are not."""
    body = client.get("/plugins/transcripts/manifest?datasource=xen&layer=tx").get_json()

    assert body["pixel_size"] is None


def test_the_pixel_size_is_read_live_rather_than_out_of_the_cache(client, project):
    """Somebody who sets the pixel size on the edit page should get a
    correctly labelled slider without rebuilding a 19-million-row cache."""
    before = client.get("/plugins/transcripts/manifest?datasource=xen&layer=tx")
    assert before.get_json()["pixel_size"] is None

    import dataclasses

    record = Project.load("xen")
    record.patch(image=dataclasses.replace(
        record.image, pixel_size={"value": 0.5, "unit": "µm"})).save()

    after = client.get("/plugins/transcripts/manifest?datasource=xen&layer=tx")
    assert after.get_json()["pixel_size"] == pytest.approx(0.5)


# -- gene groups from a file ------------------------------------------------

GROUP_CSV = ("gene,group\n"
             "EPCAM,Epithelium,Tumour\n"
             "CD3E,T cells\n"
             "PTPRC,T cells,Immune\n"
             "MADEUP,Ghosts\n")


def _post_groups(client, tmp_path, text=GROUP_CSV, name="groups.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return client.post("/plugins/transcripts/groups",
                       data={"datasource": "xen", "layer": "tx",
                             "path": str(path)})


def test_a_group_file_is_read_into_groups(client, project, tmp_path):
    """The shape Xenium Explorer's own import takes, because that is what
    people already have: the first column is a gene and every column after it
    is a group it belongs to."""
    body = _post_groups(client, tmp_path).get_json()

    assert [group["name"] for group in body["groups"]] == [
        "Epithelium", "Tumour", "T cells", "Immune"]
    assert body["groups"][0]["genes"] == ["EPCAM"]
    assert body["groups"][2]["genes"] == ["CD3E", "PTPRC"]


def test_names_the_panel_does_not_carry_are_reported_and_not_dropped(
        client, project, tmp_path):
    """A marker list written for a bigger panel is the ordinary case, and
    importing the genes that ARE here is what somebody wants -- but silently
    dropping the rest would be the import quietly doing less than it said."""
    body = _post_groups(client, tmp_path).get_json()

    assert body["unknown"] == ["MADEUP"]
    assert all("MADEUP" not in group["genes"] for group in body["groups"])


def test_a_file_with_no_header_row_reads_the_same(client, project, tmp_path):
    """A two-column CSV with no header is the commonest thing anybody exports
    out of a spreadsheet, and refusing it would be refusing the file this
    route exists to take."""
    body = _post_groups(client, tmp_path,
                        text="EPCAM,Epithelium\nCD3E,T cells\n").get_json()

    assert [group["name"] for group in body["groups"]] == [
        "Epithelium", "T cells"]


def test_gene_names_come_back_in_the_panels_own_spelling(client, project, tmp_path):
    """Matched case-insensitively and given back as the panel spells them:
    a group holding "epcam" would draw nothing and look like an empty
    group."""
    body = _post_groups(client, tmp_path,
                        text="epcam,Epithelium\n").get_json()

    assert body["groups"][0]["genes"] == ["EPCAM"]


def test_the_file_may_be_uploaded_instead_of_named(client, project):
    """On a cluster the browser is on a laptop and there is nothing local to
    name by path -- so the bytes come over instead. Same parse either way."""
    import io

    response = client.post(
        "/plugins/transcripts/groups",
        data={"datasource": "xen", "layer": "tx",
              "file": (io.BytesIO(GROUP_CSV.encode()), "groups.csv")},
        content_type="multipart/form-data")

    assert response.status_code == 200
    assert [g["name"] for g in response.get_json()["groups"]][0] == "Epithelium"


def test_a_file_that_is_not_there_is_said_rather_than_crashed(client, project):
    response = client.post("/plugins/transcripts/groups",
                           data={"datasource": "xen", "layer": "tx",
                                 "path": "/nowhere/groups.csv"})

    assert response.status_code == 400
    assert "no file at" in response.get_json()["error"]


def test_a_file_naming_no_group_at_all_is_empty_rather_than_an_error(
        client, project, tmp_path):
    """One column of genes and nothing beside them. The dialog says so; a 400
    would be reporting a broken file for one that is merely not this."""
    body = _post_groups(client, tmp_path, text="EPCAM\nCD3E\n").get_json()

    assert body["groups"] == []
    assert body["unknown"] == []


# -- aggregated points ------------------------------------------------------
#
# The level of detail Points mode uses instead of handing a zoomed-out view
# over to a density raster. The route's whole job here is to pass the three
# things an aggregate is a function of -- level, genes, threshold -- into
# `transcript_tiles.aggregate_tile` and to key the cache on all three, and the
# failure mode of getting the last part wrong is a browser that serves the
# tile for a selection the user has since changed.


def _aggregates(response):
    raw = response.get_data()
    if response.headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return np.frombuffer(raw, dtype=tt.AGGREGATE_DTYPE)


def test_the_manifest_says_how_many_bins_a_tile_is_cut_into(client, project):
    body = client.get(
        "/plugins/transcripts/manifest?datasource=xen&layer=tx").get_json()

    # The client picks its level of detail by working out how wide a bin
    # would be ON SCREEN, which it cannot do without this.
    assert body["aggregate_bins"] == tt.AGGREGATE_BINS


def test_a_level_above_zero_returns_aggregates(client, project):
    response = client.get("/plugins/transcripts/points"
                          "?datasource=xen&layer=tx&tile=0_0&level=1")

    assert response.headers["X-Transcript-Record-Size"] == "14"
    records = _aggregates(response)
    # Level 1 is one tile over the whole 256-pixel image, so every molecule
    # is inside it and none may be lost on the way through the route.
    assert records["count"].sum() == 600
    assert len(records) < 600, "nothing was merged at all"


def test_level_zero_is_still_the_molecules_themselves(client, project):
    response = client.get("/plugins/transcripts/points"
                          "?datasource=xen&layer=tx&tile=0_0")

    assert response.headers["X-Transcript-Record-Size"] == "11"
    # Every gene, and the score with it: that is what keeps toggling a gene
    # and moving the quality slider free at the zoom where somebody is
    # looking at individual molecules.
    assert set(_points(response)["gene"].tolist()) <= set(range(len(GENES)))


def test_aggregates_are_restricted_to_the_genes_asked_for(client, project):
    response = client.get("/plugins/transcripts/points"
                          "?datasource=xen&layer=tx&tile=0_0&level=1"
                          "&genes=CD3E")

    records = _aggregates(response)
    assert set(records["gene"].tolist()) == {GENES.index("CD3E")}
    assert 0 < records["count"].sum() < 600


def test_the_threshold_is_applied_before_the_merge(client, project, tmp_path):
    # Rebuilt with real scores, because a count is a count of what survived
    # the filter and the fixture's points all carry the "no score" 255.
    rng = np.random.default_rng(11)
    n = 600
    q = rng.integers(0, 41, size=n).astype(np.uint8)
    source = tmp_path / "scored.parquet"
    source.write_bytes(b"stand-in")
    tt.build("xen", "tx", genes=GENES,
             gene_index=rng.integers(0, len(GENES), size=n).astype(np.uint16),
             x=rng.uniform(0, 256, size=n).astype(np.float32),
             y=rng.uniform(0, 256, size=n).astype(np.float32),
             q=q,
             expected=tt.expected_manifest(source, width=256, height=256,
                                           tile_size=128, layer_id="tx"))

    response = client.get("/plugins/transcripts/points"
                          "?datasource=xen&layer=tx&tile=0_0&level=1&minq=30")

    assert _aggregates(response)["count"].sum() == int((q >= 30).sum())


def test_a_tile_is_keyed_on_everything_its_answer_depends_on(client, project):
    def tag(query):
        return client.get("/plugins/transcripts/points"
                          f"?datasource=xen&layer=tx&tile=0_0&{query}"
                          ).headers["ETag"]

    base = tag("level=1")
    # A browser that reused one of these for another would go on drawing a
    # gene that had been switched off, or a threshold that had been moved.
    assert tag("level=2") != base
    assert tag("level=1&genes=CD3E") != base
    assert tag("level=1&minq=30") != base
    assert tag("level=1") == base
    # And the level-0 tile is a different thing again, not the same address.
    assert tag("") != base


def test_an_aggregate_tile_is_keyed_on_how_it_was_derived(client, project,
                                                          monkeypatch):
    """An aggregate is computed per request, so the cache's mtime cannot see
    a change to the code that derives it -- and these tiles go out with a
    year-long max-age. Without the grain and the revision in the key, a
    retune of the bin grid or a fix to which molecule stands for a bin
    reaches only browsers that have never drawn the layer."""
    def tag(query):
        return client.get("/plugins/transcripts/points"
                          f"?datasource=xen&layer=tx&tile=0_0&{query}"
                          ).headers["ETag"]

    base, molecules = tag("level=1"), tag("")
    monkeypatch.setattr(tt, "AGGREGATE_BINS", tt.AGGREGATE_BINS + 1)
    assert tag("level=1") != base
    monkeypatch.setattr(tt, "AGGREGATE_REVISION", tt.AGGREGATE_REVISION + 1)
    assert tag("level=1") != base
    # Level 0 is the molecules as stored: neither of these touches it, and
    # keying it on them would throw the heaviest tiles away for nothing.
    assert tag("") == molecules


def test_a_level_that_is_not_a_number_serves_the_molecules(client, project):
    # Lenient on purpose: these arrive on every tile request from a viewer
    # that is mid-zoom, and a 400 is a hole in the picture where the honest
    # answer is the base level.
    response = client.get("/plugins/transcripts/points"
                          "?datasource=xen&layer=tx&tile=0_0&level=banana")

    assert response.status_code == 200
    assert response.headers["X-Transcript-Record-Size"] == "11"


def test_the_threshold_does_not_invalidate_a_molecule_tile(client, project):
    def tag(query):
        return client.get("/plugins/transcripts/points"
                          f"?datasource=xen&layer=tx&tile=0_0&{query}"
                          ).headers["ETag"]

    # Level 0 sends the score with every record and the client discards in
    # its shader, so the threshold changes nothing about the bytes. Keying on
    # it anyway would throw the whole cache away on each tick of a slider
    # that, at that zoom, does not affect what the server sent.
    assert tag("minq=30") == tag("minq=10") == tag("")

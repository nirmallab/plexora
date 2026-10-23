"""The Artifact Detector's cycle rule and basic QC (`run_basic_qc`).

A fold, a bubble or debris can come or go between imaging cycles, so it is
raised in a run of consecutive channels rather than in most of them; real
cells bright in one cycle's markers keep the same nuclei in every cycle.
Basic QC writes what the model-free checks find as automatic, unreviewed
regions on an image nobody has QC'd, and never over a QC result."""

import numpy as np

from plexora.agent import AgentSession, invoke, jobs, registry
from plexora.ai.qc_scenes import cyclic_scene
from plexora.plugins.qc.server import artifacts as art
from tests.qc_fixtures import make_qc_project


def _coarse_maps(shape=(40, 40), cycles=4, size=4):
    share = np.zeros((cycles,) + shape, dtype=np.float32)
    dna = np.full((cycles,) + shape, 8.0, dtype=np.float32)
    dna += np.random.default_rng(0).normal(0, 0.05, dna.shape).astype(np.float32)
    return {"share": share, "dna": dna, "markers": [size - 1] * cycles,
            "sizes": [size] * cycles, "index": list(range(1, cycles + 1)), "shape": shape}


def test_a_run_of_cycles_whose_nuclei_changed_is_a_seed_and_bright_cells_are_not():
    maps = _coarse_maps()
    # An artifact from cycle 3 on: its markers raised and its nuclei doubled.
    maps["share"][2:, 5:15, 5:15] = 1.0
    maps["dna"][2:, 5:15, 5:15] += np.log(2.2)
    # Cells bright in all of cycle 1's markers, with the nuclei of every cycle.
    maps["share"][0, 25:35, 25:35] = 1.0
    core = np.ones((40, 40), dtype=bool)
    found = art.cycle_hits(maps, core, art.PARAMS_DEFAULT)
    assert found["seed"][5:15, 5:15].all()
    # A stray grid cell of noise may pass; the detector's opening and its
    # least area take it out. The aggregate as a place is not a seed.
    assert found["seed"][25:35, 25:35].mean() < 0.05
    assert found["hit"][2, 10, 10] and found["hit"][3, 10, 10] and not found["hit"][0, 10, 10]
    assert found["strength"][10, 10] > art.PARAMS_DEFAULT["cycle_nuclear_z"]
    # Too few channels in the run: one cycle of two channels is not enough.
    small = _coarse_maps(cycles=3, size=2)
    small["share"][1, 5:15, 5:15] = 1.0
    small["dna"][1, 5:15, 5:15] += np.log(2.2)
    assert not art.cycle_hits(small, core, art.PARAMS_DEFAULT)["seed"].any()


def _detect(project):
    session = AgentSession()
    fp, context = art.plan(session, project)
    return art.run(session, project, fp, context)["summary"]


def test_the_detector_finds_debris_in_one_cycle_and_leaves_a_dense_aggregate(tmp_path):
    info = make_qc_project(tmp_path, "cyc", scene=cyclic_scene, grid=48,
                           artifacts=("cycle_debris", "dense_aggregate"))
    summary = _detect("cyc")
    assert summary["cycle_rule"]["active"] and len(summary["cycle_rule"]["cycles"]) == 4
    debris = next(r["mask"] for r in info["truth"]["regions"] if r["name"] == "cycle_debris")
    aggregate = info["truth"]["biology"]["dense_aggregate"]
    kept = [o for o in summary["objects"] if o["score"] >= art.AUTO_THRESHOLD]

    def inside(o, mask):
        x, y = o["centroid"]
        return bool(mask[int(y), int(x)])

    found = [o for o in kept if inside(o, debris)]
    assert found and found[0]["metrics"]["rule"] == "cycle"
    assert found[0]["metrics"]["cycles"] == [2]
    assert set(found[0]["channels"]) <= {"DNA_2", "M2a", "M2b", "M2c"}
    assert not [o for o in kept if inside(o, aggregate)]


def test_without_known_cycles_only_the_most_channels_rule_runs(tmp_path):
    make_qc_project(tmp_path, "two", artifacts=("fold",))
    plain = AgentSession()
    names = art.channel_names(plain.project("two"))
    groups, why = art.cycle_groups("two", names[:1] + names[2:3])
    assert groups is None and why
    # The bench scene's cycles hold 3 and 2 channels: within 2 to 20.
    groups, why = art.cycle_groups("two", names)
    assert [len(c["channels"]) for c in groups] == [3, 2] and why is None


def _basic(project):
    started = invoke(AgentSession(), "qc.basic_run", {"project": project})
    assert started["ok"], started
    jobs.drain(600)
    return jobs.store().get(started["result"]["job_id"])


def test_basic_qc_writes_automatic_cycle_regions_and_never_over_a_qc_result(tmp_path):
    from plexora.plugins.qc.server import results

    registry.discover(["qc", "roi"])
    info = make_qc_project(tmp_path, "cyc", scene=cyclic_scene, grid=48,
                           artifacts=("cycle_debris",))
    job = _basic("cyc")
    assert job["status"] == "done", job
    out = job["result"]
    assert out["ran"] == ["artifacts", "dna_retention"] and out["regions"]["fold"] == 1
    assert "Nobody reviewed it" in out["note"]
    active = results.active(results.load("cyc"))
    assert active["origin"] == results.BASIC_ORIGIN
    (region,) = active["candidates"].values()
    assert region["origin"] == "automatic" and region["created_by"] == "basic_qc"
    assert region["scope"] == "cycle" and region["cycles"] == [2]
    assert region["channels"] == ["DNA_2", "M2a", "M2b", "M2c"]
    assert region["user_state"]["approved"] is False
    # Its cells are left out (a fold's region fails the whole cell).
    from plexora.agent import cell_exclusions

    debris = next(r["mask"] for r in info["truth"]["regions"] if r["name"] == "cycle_debris")
    inside = {c["id"] for c in info["cells"] if debris[int(c["y"]), int(c["x"])]}
    left_out = {int(i) for i in cell_exclusions.current(AgentSession().data("cyc"))
                .ids_for("M2a")}
    assert inside <= left_out
    # Run again: the same result, its region replaced, not doubled.
    assert _basic("cyc")["status"] == "done"
    assert len(results.active(results.load("cyc"))["candidates"]) == 1

    # An image someone QC'd is left alone.
    make_qc_project(tmp_path, "done", scene=cyclic_scene, grid=48, artifacts=("cycle_debris",))
    with results.lock("done"):
        document = results.load("done")
        results.ensure_active(document, "done")
        results.save("done", document)
    out = _basic("done")["result"]
    assert out["ran"] == [] and "QC result already" in out["skipped"]["all"]

"""Engine v4: a dataset's images gated as groups of aligned images.

The partition is pure (`groups.partition` on profile dicts): which images
share a decision, which is the scale image, which anchors the looks rotate
over. The session tests drive a synthetic dataset to the end with the truth
oracle and pin what v4 adds: one decision per group, pooled looks drawn from
more than one image, carried gates written as `group_aligned`, bounded
checks, the per-image progress a viewer's image switcher reads.
"""

import json

import numpy as np
import pytest

from plexora.agent import AgentSession, invoke, jobs, registry
from plexora.plugins.gating.server.autogate import groups, reference, schemas

QGRID = (1.0, 5.0, 10.0, 25.0, 50.0, 75.0, 90.0, 95.0, 99.0, 99.9)


def prof(mu=1.0, sd=0.2, pf=0.2, n=5000, klass="bimodal", seed=0, flags=(), s2b=3.0):
    """A profiled unit's numbers: a background N(mu, sd) and, `pf` of the
    cells, positives five background sds above it."""
    rng = np.random.default_rng(seed)
    n_pos = int(round(n * pf))
    values = np.concatenate([rng.normal(mu, sd, n - n_pos), rng.normal(mu + 5 * sd, sd, n_pos)])
    quantiles = {f"p{p:g}": float(np.percentile(values, p)) for p in QGRID}
    return {"class": klass, "fit_space": "linear", "flags": list(flags),
            "metrics": {"mu_bg": mu, "sd_bg": sd, "fraction": pf},
            "summary": {"quantiles_raw": quantiles, "positive_fraction": pf, "n_cells": n,
                        "signal_to_background": s2b}}


def one_mode(mu=1.0, sd=0.2, n=5000, seed=0):
    return prof(mu=mu, sd=sd, pf=0.0, n=n, klass="unimodal", seed=seed)


# -- alignment ---------------------------------------------------------------------


def test_align_bg_recovers_a_painted_shift_and_contrast():
    scale = prof(mu=1.0, sd=0.2)
    target = prof(mu=1.3, sd=0.3, seed=1)
    al = reference.align_bg(target, scale)
    assert al["b"] == pytest.approx(1.5)
    assert al["a"] == pytest.approx(1.3 - 1.5 * 1.0)
    assert al["shift_bg_sd"] == pytest.approx(1.5)
    assert al["rms_bg"] < 0.15 and al["basis"] == "mixture"
    # carried there and back is the identity
    gate = reference.carry(al, 2.0, "linear", "linear")
    assert gate == pytest.approx(1.3 + 1.5 * 1.0)
    assert reference.carry_back(al, gate, "linear", "linear") == pytest.approx(2.0)


def test_a_one_mode_image_is_read_robustly():
    bg = reference.background(one_mode(mu=2.0, sd=0.5, n=20000))
    assert bg["basis"] == "robust"
    assert bg["mu"] == pytest.approx(2.0, abs=0.02)
    assert bg["sd"] == pytest.approx(0.5, abs=0.03)


def test_mappable_needs_a_contrast_ratio_in_range():
    base = {"a": 0.0, "shift_bg_sd": 0.0, "rms_bg": 0.0}
    assert reference.mappable({**base, "b": 1.0})
    assert not reference.mappable({**base, "b": 2.5})
    assert not reference.mappable({**base, "b": 0.4})
    assert not reference.mappable({**base, "b": 1.0, "rms_bg": 0.5})
    assert not reference.mappable(None)


# -- the partition -----------------------------------------------------------------


def test_two_aligned_images_are_one_group_and_both_anchors():
    out = groups.partition({"a": prof(seed=1), "b": prof(seed=2, n=6000)}, order=["a", "b"])
    assert out["singletons"] == {}
    (group,) = out["groups"]
    assert sorted(group["members"]) == ["a", "b"]
    assert sorted(group["anchors"]) == ["a", "b"] and group["anchors"][0] == group["scale"]
    assert group["classes"][[p for p in "ab" if p != group["scale"]][0]] == "stable"


def test_the_users_reference_image_is_the_scale_image_inside_its_group():
    profiles = {"a": prof(seed=1), "b": prof(seed=2), "c": prof(seed=3)}
    out = groups.partition(profiles, reference_image="c", order=["a", "b", "c"])
    assert out["groups"][0]["scale"] == "c"
    assert out["groups"][0]["anchors"][0] == "c"


def test_a_shifted_image_joins_its_group_and_a_far_one_is_gated_alone():
    profiles = {"a": prof(mu=1.0, seed=1), "b": prof(mu=1.12, seed=2),
                "c": prof(mu=1.0, sd=0.6, seed=3)}
    out = groups.partition(profiles, order=["a", "b", "c"])
    (group,) = out["groups"]
    assert sorted(group["members"]) == ["a", "b"]
    other = "b" if group["scale"] == "a" else "a"
    assert group["classes"][other] == "member_shifted"
    # three times the background spread: the gate does not carry
    assert out["singletons"] == {"c": "distribution_change"}


def test_two_images_that_do_not_align_are_each_gated_alone():
    out = groups.partition({"a": prof(sd=0.2, seed=1), "b": prof(sd=0.7, seed=2)},
                           order=["a", "b"])
    assert out["groups"] == []
    assert set(out["singletons"]) == {"a", "b"}


def test_two_batches_far_apart_are_two_groups():
    profiles = {f"lo{i}": prof(mu=1.0, seed=i) for i in range(4)}
    profiles.update({f"hi{i}": prof(mu=4.0, seed=10 + i) for i in range(4)})
    out = groups.partition(profiles, order=sorted(profiles))
    assert out["singletons"] == {}
    members = sorted(sorted(g["members"]) for g in out["groups"])
    assert members == [[f"hi{i}" for i in range(4)], [f"lo{i}" for i in range(4)]]
    for group in out["groups"]:
        # each re-aligned onto its own scale image
        assert all(abs(group["alignments"][p]["shift_bg_sd"]) < 0.5 for p in group["members"])


def test_a_failed_stain_is_alone_and_never_the_origin():
    profiles = {"a": prof(seed=1), "b": prof(seed=2), "c": prof(seed=3),
                "bad": prof(seed=4, flags=("empty_channel",))}
    out = groups.partition(profiles, order=["bad", "a", "b", "c"])
    assert out["singletons"] == {"bad": "staining_failure"}
    (group,) = out["groups"]
    assert "bad" not in group["members"] and group["origin"] != "bad"


def test_a_tma_carries_its_empty_cores_and_attaches_its_small_ones():
    cores = {f"core{i:02d}": prof(mu=1.0 + 0.02 * i, n=3000, seed=i) for i in range(10)}
    cores["empty"] = one_mode(mu=1.05, n=3000, seed=40)
    # A small core whose background is a little off: alone it would be
    # gated on its own; under the cell floor it is attached instead.
    cores["tiny"] = prof(mu=1.45, sd=0.24, n=400, seed=41)
    out = groups.partition(cores, order=sorted(cores))
    assert out["singletons"] == {}
    (group,) = out["groups"]
    assert len(group["members"]) == 12
    assert group["classes"]["empty"] == "no_positives"
    assert group["flags"]["tiny"] == ["attached_under_floor"]
    # neither leads a look
    assert "empty" not in group["anchors"] and "tiny" not in group["anchors"]
    assert len(group["anchors"]) == groups.GROUPS["anchors_max"]


def test_anchors_span_the_groups_shifts():
    profiles = {f"s{i}": prof(mu=1.0 + 0.04 * i, seed=i) for i in range(9)}
    out = groups.partition(profiles, reference_image="s4", order=sorted(profiles))
    (group,) = out["groups"]
    assert group["scale"] == "s4"
    shifts = {p: group["alignments"][p]["shift_bg_sd"] for p in group["members"]}
    others = [p for p in group["anchors"] if p != "s4"]
    assert min(shifts, key=shifts.get) in others and max(shifts, key=shifts.get) in others


# -- sessions ------------------------------------------------------------------------

@pytest.fixture
def _gating():
    registry.discover(["gating"])


def _ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    return result["result"]


def _start(session, **options):
    started = _ok(invoke(session, "gating_session_start",
                         {"scope": "dataset", "dataset": "cohort", **options}))
    jobs.drain(240)
    return started


@pytest.mark.pro
def test_engine_names_and_scope_defaults():
    from plexora.plugins.gating.capabilities_session import SessionOptions

    assert schemas.ENGINES == ("v1", "v3", "v4")
    assert SessionOptions(scope="project", project="p").engine == "v3"
    assert SessionOptions(scope="dataset", dataset="d").engine == "v4"
    assert SessionOptions(scope="project", project="p", engine="v2").engine == "v3"
    assert SessionOptions(engine="candidates").engine == "v3"
    assert SessionOptions(scope="dataset", dataset="d", engine="v4").basic_qc is True
    assert SessionOptions(scope="dataset", dataset="d", engine="v4").display_check == "off"
    assert schemas.panel_engine("v2") and schemas.panel_engine("v4")
    assert not schemas.panel_engine("v1")


@pytest.mark.pro
def test_a_dataset_is_gated_by_groups(tmp_path, _gating):
    from tests.autogate_fixtures import answer_review, make_gating_dataset
    from tests.test_gating_session import Oracle, drive

    made = make_gating_dataset(tmp_path, shifts=(0.0, 0.0, 0.5), grid=20, size=800,
                               markers=("CD3", "CD8", "CD20", "CD4", "FOXP3"))
    session = AgentSession()
    agents = {name: Oracle(info) for name, info in made.items()}

    class ByImage:
        def answer(self, packet):
            project = packet["units"][0]["project"] if packet["units"] else "cohort_1"
            return agents[project].answer(packet)

    started = _start(session)
    sid = started["session_id"]
    answer_review(session, started)
    packets = drive(session, sid, ByImage(), limit=160)
    status = _ok(invoke(session, "gating_session_status", {"detail": "full", "session_id": sid}))
    by_unit = {(u["project"], u["marker"]): u for u in status["units"]}
    for (project, marker), unit in by_unit.items():
        assert unit["state"] in schemas.TERMINAL_STATES, unit
        if unit["state"] in schemas.ACCEPTED_STATES:
            assert agents[project].excess(marker, unit["final"]) <= max(
                3, 0.02 * len(made[project]["cells"])), unit
    summary = status["dataset"]
    assert summary["engine"] == "v4"
    grouped = [m for m, row in summary["markers"].items() if row["groups"]]
    assert grouped, summary
    # A marker gated as a group: a scale image, members carried as
    # `group_aligned`, and its gate's provenance says from where.
    gates = {p: _ok(invoke(session, "get_all_gates", {"project": p})) for p in made}
    methods = {(p, m): gates[p]["provenance"][m]["method"] for p in made
               for m in made[p]["truth"] if m in gates[p]["provenance"]}
    assert "group_aligned" in set(methods.values()), methods
    carried = [(p, m) for (p, m), method in methods.items() if method == "group_aligned"]
    p, m = carried[0]
    provenance = _ok(invoke(session, "get_gate_provenance", {"project": p}))
    detail = json.dumps(provenance, default=str)
    assert f"group:{m}:" in detail and "alignment" in detail
    # No marker spent more packets than the reference-first path at best.
    ceiling = groups.GROUPS["packets_base"] + len(made)
    per_marker = {}
    for packet in packets:
        for ref in packet.get("units") or ():
            per_marker.setdefault(ref["marker"], set()).add(packet["packet_id"])
    assert all(len(ids) <= ceiling + len(made) for ids in per_marker.values()), per_marker
    # The brief names each marker's groups; progress says where each image is.
    brief = _ok(invoke(session, "gating_session_status", {"session_id": sid}))
    assert set(brief["groups"]) == set(summary["markers"])
    dataset = status["progress"]["dataset_progress"]
    assert set(dataset["images"]) == set(made)
    assert dataset["counts"]["images_total"] == len(made)
    assert all(row["state"] in ("done", "review") for row in dataset["images"].values())
    # The report lists each marker's groups, in the HTML and the PDF.
    report = _ok(invoke(session, "gating_report", {"session_id": sid}))
    html = open(report["paths"]["html"], encoding="utf-8").read()
    assert "Groups of aligned images" in html and grouped[0] in html
    assert open(report["paths"]["pdf"], "rb").read(4) == b"%PDF"


@pytest.mark.pro
def test_a_pooled_look_is_drawn_from_another_image_and_read_there(tmp_path, _gating):
    """A group's looks rotate over its anchors: a look from the second image
    names that image, and its answer goes to the scale image's decision. The
    reader calls the scale images' audit-sheet rows suspicious, so each
    group decides with cell panels."""
    from plexora.plugins.gating.server.autogate import engine as engines
    from tests.autogate_fixtures import answer_review, make_gating_dataset
    from tests.test_gating_session import Oracle

    made = make_gating_dataset(tmp_path, shifts=(0.0, 0.1), grid=20, size=800,
                               markers=("CD3", "CD8", "CD20", "CD4", "FOXP3"))
    session = AgentSession()
    agents = {name: Oracle(info) for name, info in made.items()}
    started = _start(session)
    sid = started["session_id"]
    answer_review(session, started)
    looks = []
    result = _ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 20}))
    for _ in range(200):
        if result["state"] != "decision":
            break
        packet = result["packet"]
        if packet["kind"] == "cell_panel":
            looks.append(packet["units"][0])
        project = packet["units"][0]["project"] if packet["units"] else "cohort_1"
        reply = agents[project].answer(packet)
        if packet["kind"] == "t1_strip":
            record = engines.store().load(sid)
            for marker in reply["verdicts"]:
                unit = record["units"][engines.unit_key(project, marker)]
                if (unit.get("group") or {}).get("role") == "scale":
                    reply["verdicts"][marker] = "suspicious"
        answered = _ok(invoke(session, "gating_answer", {
            "session_id": sid, "packet_id": packet["packet_id"], "answer": reply}))
        result = answered["next"]
    assert result["state"] in ("decided", "done"), result
    pooled = [ref for ref in looks if ref.get("unit")]
    for ref in pooled:
        deciding = ref["unit"].split("::")[0]
        assert deciding != ref["project"]
        assert ref["unit"].endswith(f"::{ref['marker']}")
    # Every grouped marker's members were carried once its scale image closed.
    record = engines.store().load(sid)
    for marker, entry in record["groups"].items():
        for group in entry["groups"]:
            assert group["state"] in ("carried", "closed"), group
    assert all(u["state"] in schemas.TERMINAL_STATES for u in record["units"].values())


@pytest.mark.pro
def test_v4_on_one_image_is_v3(tmp_path, _gating):
    """The guard that v4 cannot regress single-image gating: on one image it
    is v3, gate for gate and look for look."""
    from tests.autogate_fixtures import answer_review, make_gating_project
    from tests.test_gating_session import HARD, Oracle, drive

    runs = {}
    for engine in ("v3", "v4"):
        name = f"g{engine}"
        info = make_gating_project(tmp_path, name, grid=24, size=1024, markers=HARD)
        session = AgentSession()
        started = _ok(invoke(session, "gating_session_start", {
            "engine": engine, "scope": "project", "project": name}))
        jobs.drain(240)
        answer_review(session, started)
        packets = drive(session, started["session_id"], Oracle(info), limit=120)
        status = _ok(invoke(session, "gating_session_status", {
            "detail": "full", "session_id": started["session_id"]}))
        runs[engine] = ({u["marker"]: (u["state"], u.get("final")) for u in status["units"]},
                        [(p["kind"], [r["marker"] for r in p.get("units") or ()])
                         for p in packets])
    assert runs["v4"][0] == runs["v3"][0]
    assert runs["v4"][1] == runs["v3"][1]


@pytest.mark.pro
def test_the_bulk_pass_reads_the_same_with_one_image_at_a_time_or_three(tmp_path, monkeypatch,
                                                                        _gating):
    """Images profiled in parallel settle exactly as one at a time: the same
    first decisions, mixture gates, classes and groups."""
    from plexora.plugins.gating.server.autogate import bulk
    from plexora.plugins.gating.server.autogate import engine as engines
    from tests.autogate_fixtures import make_gating_dataset

    seen = {}
    for workers, name in ((1, "serial"), (3, "parallel")):
        monkeypatch.setattr(bulk, "IMAGE_WORKERS", workers)
        make_gating_dataset(tmp_path, name=name, shifts=(0.0, 0.2, 0.5), grid=16, size=768)
        session = AgentSession()
        started = _ok(invoke(session, "gating_session_start",
                             {"scope": "dataset", "dataset": name}))
        jobs.drain(240)
        record = engines.store().load(started["session_id"])
        assert record["bulk_images"] == {p: "profiled" for p in record["images"]}
        seen[name] = (
            {k.replace(name, "X"): (u["state"], u.get("gmm"), u.get("class"),
                                    (u.get("group") or {}).get("role"))
             for k, u in record["units"].items()},
            {m: [(g["scale"].replace(name, "X"), sorted(p.replace(name, "X")
                                                          for p in g["members"]))
                 for g in entry["groups"]]
             for m, entry in (record.get("groups") or {}).items()})
    assert seen["serial"] == seen["parallel"]

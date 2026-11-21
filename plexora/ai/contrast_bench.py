"""The contrast-check bench: how well a model reads the auto-contrast pictures
(`gating.display`, the `display_setup` packet).

Three synthetic scenes with per-cell causes (`bench_data.CONTRAST_SCENARIOS`:
spill from a neighbour, local background, dim positives; a negative cell
carries no stain of its own) are put through
Auto Contrast (`contrast_session_start`) as a user would run it; each
image's contrast packet -- the grid of five brightnesses and the strip of
bright and dim tiles -- goes to the model, and its answer is scored on what
the scene knows:

* tiles      the strip's tiles, each judged against its cell's truth: a dim
             tile is stained (`dim_stained`) when its cell is truly positive,
             a bright tile is debris (`bright_artifact`) when it is not.
             Balanced accuracy -- the mean of the share right on truly
             stained and on truly unstained tiles -- so saying "stained"
             every time scores 0.5, not the share of stained tiles.
* structure  the grid's columns: `structure_ok` against what the grid
             measured (`display_grid` levels, `calibration.VETOES`): detail
             shows where under 5 % of the bright cells is at white and their
             brightest is at least 0.3. Code vetoes the rest anyway, so this
             is reported beside the tiles, not a bar of its own.

The background question is not scored: the synthetic background draws black
at every column, so no column is right.

`plexora ai route-bench PROVIDER/MODEL --feature gating.display` asks through
the gateway's dev route; `plexora ai contrast-bench --write DIR` writes each
packet and its pictures for any model to answer outside the gateway (no
credits), and `--score DIR` scores the answers left there.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import numpy as np

#: (scene, seed): spill from a neighbour, dim positives, and both with a
#: patch of haze (`bench_data.CONTRAST_SCENARIOS`: negatives carry no stain).
SCENES = (("contrast_spill", 0), ("contrast_dim", 1), ("contrast_mixed", 2))
MARKERS = ("CD3", "CD8", "CD20", "CD4", "FOXP3")
FEATURE = "gating.display"
VERSION = "contrast-1"


def collect(scenes=SCENES, *, size=1024) -> list:
    """One item per scene: {scene, packet, images [{role, png}], truth
    {marker: {tile: stained}}, levels {marker: {column: levels}}} -- the
    contrast packet Auto Contrast sends for it, in a throwaway data root."""
    from plexora import paths
    from plexora.agent import AgentSession, artifacts, invoke, jobs, registry
    from plexora.ai import bench, bench_data

    out = []
    previous = {k: os.environ.get(k) for k in ("PLEXORA_DATA_PATH", "BIOCOGNIA_MARKER_GRAPH")}
    with tempfile.TemporaryDirectory(prefix="plexora-contrast-bench-", ignore_cleanup_errors=True) as root:
        os.environ["PLEXORA_DATA_PATH"] = root
        os.environ["BIOCOGNIA_MARKER_GRAPH"] = str(Path(root) / "graph")
        paths.reset()
        try:
            registry.discover(["gating"])
            session = AgentSession(table_limit=4)
            for scene, seed in scenes:
                name = f"contrast_{scene}"
                made = bench_data.register_spatial(root, name, scenario=scene, size=size,
                                                   seed=seed, markers=MARKERS)
                helper = bench.TruthAgent(made["values"], made["truth"], "oracle", seed=seed,
                                          ids=made.get("ids"))
                started = bench._ok(invoke(session, "contrast_session_start",
                                           {"scope": "project", "project": name}))
                jobs.drain(600)
                sid = started["session_id"]
                result = bench._ok(invoke(session, "gating_next", {"session_id": sid, "wait_s": 30}))
                for _ in range(40):
                    if result["state"] != "decision":
                        break
                    packet = result["packet"]
                    if packet["kind"] == "display_setup":
                        out.append(_item(scene, packet, made, artifacts))
                        break
                    # The set-up before the grid is not what is benched: the truth reader answers it.
                    result = bench._ok(invoke(session, "gating_answer", {
                        "session_id": sid, "packet_id": packet["packet_id"],
                        "answer": helper.answer(packet)}))["next"]
                invoke(session, "gating_session_finish", {"session_id": sid, "action": "close"})
            session.close()
        finally:
            jobs.drain(120)
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            paths.reset()
    return out


def _item(scene, packet, made, artifacts) -> dict:
    truth, values, levels, images = {}, {}, {}, []
    rows = None if made.get("ids") is None else {int(i): r for r, i in enumerate(made["ids"])}
    for image in packet.get("images") or []:
        png, sidecar = artifacts.get(image["artifact_id"])
        images.append({"role": image.get("role"), "png": png})
        for row in (sidecar.get("manifest", sidecar)).get("rows") or []:
            marker = row["marker"]
            if "levels" in row:
                levels[marker] = row["levels"]
            for tile, cell in (row.get("tiles") or {}).items():
                cid = int(cell["cell_id"])
                index = rows.get(cid, -1) if rows is not None else cid - 1
                known = made["truth"].get(marker)
                if known is not None and 0 <= index < len(known):
                    truth.setdefault(marker, {})[tile] = bool(known[index])
                    values.setdefault(marker, {})[tile] = round(float(made["values"][marker][index]), 1)
    shown = {k: v for k, v in packet.items() if k not in ("images", "units") and not k.startswith("_")}
    return {"scene": scene, "packet": shown, "images": images, "truth": truth, "values": values,
            "levels": levels}


def structure_truth(level: dict) -> bool:
    """Whether a column shows the bright cells' detail, by the grid's own
    numbers: under `clipped_share` at white, and at least `bright_level`."""
    from plexora.agent.evidence import calibration

    return level.get("clipped_share", 0.0) <= calibration.VETOES["clipped_share"] \
        and level.get("bright_level", 1.0) >= calibration.VETOES["bright_level"]


def score(items: list, answers: dict) -> dict:
    """{metrics, rows}: `answers` is {scene: the model's answer (dict or JSON
    text), or None when the call failed}."""
    from pydantic import ValidationError

    from plexora.plugins.gating.server.autogate import answers as answer_models

    hits = {True: [], False: []}
    structure, rows = [], []
    invalid = failed = 0
    for item in items:
        said = answers.get(item["scene"])
        if said is None:
            failed += 1
            said = {}
        try:
            if isinstance(said, str):
                said = json.loads(said)
            parsed = answer_models.DisplaySetupAnswer.model_validate({**said, "kind": "display_setup"})
        except (ValueError, TypeError, ValidationError):
            invalid += 1 if said else 0
            parsed = None
        for marker, tiles in item["truth"].items():
            debris = set((parsed.bright_artifact.get(marker) if parsed else None) or ())
            stained_dim = set((parsed.dim_stained.get(marker) if parsed else None) or ())
            for tile, own in tiles.items():
                called = tile not in debris if tile.startswith("b") else tile in stained_dim
                ok = parsed is not None and called == own
                hits[own].append(ok)
                if not ok:
                    rows.append({"scene": item["scene"], "marker": marker, "tile": tile,
                                 "value": (item.get("values") or {}).get(marker, {}).get(tile),
                                 "stained": own, "said_stained": called if parsed else None})
        for marker, columns in item["levels"].items():
            seen = set((parsed.structure_ok.get(marker) if parsed else None) or ())
            structure += [parsed is not None and (cid in seen) == structure_truth(level)
                          for cid, level in columns.items()]
    shares = [float(np.mean(v)) for v in hits.values() if v]
    n = len(items) or 1
    return {"metrics": {"tile_balanced_accuracy": round(float(np.mean(shares)), 4) if shares else 0.0,
                        "tiles_stained_right": round(float(np.mean(hits[True])), 4) if hits[True] else None,
                        "tiles_unstained_right": round(float(np.mean(hits[False])), 4) if hits[False] else None,
                        "tiles": sum(len(v) for v in hits.values()),
                        "structure_agreement": round(float(np.mean(structure)), 4) if structure else 0.0,
                        "invalid_answer_rate": round(invalid / n, 4), "failure_rate": round(failed / n, 4)},
            "rows": rows}


def bench_route(route: str, *, gateway=None, items=None) -> dict:
    """Ask `route` (provider/model) each scene's contrast packet through the
    gateway's dev route and score it; returns the evaluation to submit."""
    from spatialagents.wire import ModelRequest, canonical, image_block, text_block

    from plexora.ai import tasks
    from plexora.ai.harness import prefix, route_bench, schema
    from plexora.ai.harness.gateway import GatewayClient, GatewayError

    provider, model = route_bench.split_route(route)
    gateway = gateway or GatewayClient(dev=True)
    if not gateway.dev:
        raise ValueError("The route bench runs on the dev route (an internal testing account).")
    spec = tasks.tasks()[FEATURE]
    items = collect() if items is None else items
    answers, charged, started = {}, 0, time.perf_counter()
    for item in items:
        blocks = [image_block(i["png"], "png") for i in item["images"]]
        blocks.append(text_block(canonical(item["packet"])))
        request = ModelRequest(capability=spec.capability, system=prefix.gating_prefix(),
                               messages=[{"role": "user", "content": blocks}], max_tokens=spec.max_tokens,
                               output_schema=schema.for_packet("display_setup", FEATURE, "gating",
                                                               item["packet"]),
                               context={"feature": "gating", "agent": "contrast_bench",
                                        "workflow": "auto_contrast", "attempt": 1},
                               model=route, task=FEATURE)
        try:
            response = gateway.messages(request, idempotency_key=f"contrast-bench.{time.time_ns()}")
            answers[item["scene"]], charged = response.text, charged + response.charged_micro
        except GatewayError:
            answers[item["scene"]] = None
    scored = score(items, answers)
    n = len(items) or 1
    measured = {**scored["metrics"], "cost_micro_per_image": int(charged / n),
                "seconds_per_image": round((time.perf_counter() - started) / n, 1)}
    return {"feature": FEATURE, "capability": spec.capability, "provider": provider, "model": model,
            "bench_version": VERSION, "metrics": measured, "rows": scored["rows"],
            "dataset_ids": [f"synthetic:{s}:seed{seed}" for s, seed in SCENES],
            "plexora_version": route_bench._version(), "images": []}


# -- outside the gateway -------------------------------------------------------------


def write(directory) -> list:
    """Each scene's packet as `<scene>/question.txt` beside its pictures
    (`<scene>/<role>.png`), and what scoring needs as `items.json`: for any
    model to answer outside the gateway, into `<scene>/answer.json`."""
    from spatialagents.wire import canonical

    from plexora.plugins.gating.server.autogate import answers as answer_models

    directory = Path(directory)
    items = collect()
    shape = canonical(answer_models.DisplaySetupAnswer.model_json_schema())
    for item in items:
        folder = directory / item["scene"]
        folder.mkdir(parents=True, exist_ok=True)
        names = []
        for image in item["images"]:
            (folder / f"{image['role']}.png").write_bytes(image["png"])
            names.append(f"{image['role']}.png")
        (folder / "question.txt").write_text(
            "Look at the pictures " + ", ".join(names) + " in this folder (in that order), then "
            "answer this packet.\n\n" + canonical(item["packet"])
            + "\n\nReply with one JSON object only, of this schema:\n" + shape + "\n", encoding="utf-8")
    (directory / "items.json").write_text(json.dumps(
        [{k: v for k, v in item.items() if k != "images"} for item in items], indent=1), encoding="utf-8")
    return items


def score_directory(directory) -> dict:
    """Score the `<scene>/answer.json` files a model left under `write`'s folder."""
    directory = Path(directory)
    items = json.loads((directory / "items.json").read_text(encoding="utf-8"))
    answers = {}
    for item in items:
        path = directory / item["scene"] / "answer.json"
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        start, end = text.find("{"), text.rfind("}")
        answers[item["scene"]] = text[start:end + 1] if start >= 0 < end else None
    return score(items, answers)

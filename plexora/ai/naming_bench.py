"""The marker-naming bench: how well a model names panel markers Plexora's
vocabulary does not know (`gating.marker_naming`).

Nineteen names as labs write them -- a cycle, a fluorophore or a clone glued
on, an antibody clone alone, or nothing a reader could know -- are put to the
model as the one `marker_naming` packet a session would send
(`packets.naming_packet`). The answer is scored by what Plexora does with it
(`transitions.names_said`, then `context.build`), not by its wording:

* right    the marker ends up as the one it is (or, for a name nobody could
           read, is left unnamed);
* unnamed  it is left for the description step: `unknown`, or a name the
           vocabulary does not know. Safe, only less useful;
* wrong    it ends up as another marker, and so takes that marker's
           partners. The error the bar allows none of.

Two ways to run it. `plexora ai route-bench PROVIDER/MODEL --feature
gating.marker_naming` asks through the gateway's dev route, and its result
can be submitted as the task's evaluation. `plexora ai naming-bench --packet`
prints the question for any model to answer outside the gateway (no credits),
and `--score FILE` scores that answer.
"""

from __future__ import annotations

import json
import time

#: (the name as a panel spells it, the marker it is; None: nothing tells).
CASES = (
    ("Cycle4_PanCK_AF555", "PanCK"),
    ("c2_Ki-67_MIB1", "Ki67"),
    ("R1_HLA-DR_DyLight650", "HLADR"),
    ("L26", "CD20"),                    # clones
    ("C8-144B", "CD8"),
    ("2GV6_Opal570", "CD3"),            # SP7, the bench's first CD3 clone, is Osterix's gene in graph 2026-11-21.2
    ("E1L3N", "PDL1"),
    ("NAT105", "PD1"),
    ("Forkhead-box-P3_Cy5", "FOXP3"),   # Foxp-3 is a listed spelling since graph 2026-11-21.2
    ("alpha-SMA_A488", "aSMA"),
    ("Vimentin(D21H3)", "Vimentin"),
    ("aCD68_KP1", "CD68"),
    ("Granzyme-B_cyc5", "GZMB"),
    ("Melan-A_A647", "MART1"),
    ("E-Cad_cyc3", "Ecad"),
    ("AF_488_bg", "Autofluorescence"),
    ("Channel_7", None),
    ("XQ-445", None),
    ("Ab_lot_2231", None),
)
FEATURE = "gating.marker_naming"
#: naming-2 (2026-11-22): SP7 -> 2GV6 and Foxp-3 -> Forkhead-box-P3, two names
#: graph 2026-11-21.2 resolves before the model sees them; 19 names again.
VERSION = "naming-2"


def expected() -> dict:
    """{name as spelled: the vocabulary's canonical name, or None}."""
    from plexora.ai import vocabulary

    return {name: vocabulary.canonical(meant) if meant else None for name, meant in CASES}


def packet() -> dict:
    """The `marker_naming` packet for the bench's panel, as a session sends it."""
    from plexora.plugins.gating.server.autogate import context, packets

    built = packets.naming_packet(context.build([name for name, _ in CASES]))
    if built is None:
        raise RuntimeError("The vocabulary already knows every bench name; nothing to ask.")
    return {"packet_id": "naming_bench", "kind": "marker_naming", **built}


def prompt(asked: dict) -> str:
    """The packet as text, with the answer's shape: for a model asked outside
    the gateway, which sends the shape as a schema instead."""
    from plexora.plugins.gating.server.autogate import answers
    from spatialagents.wire import canonical

    shown = {k: v for k, v in asked.items() if not k.startswith("_")}
    shape = answers.MarkerNamingAnswer.model_json_schema()
    return (canonical(shown) + "\n\nReply with one JSON object only, of this schema:\n" + canonical(shape))


def score(answer, asked: dict | None = None) -> dict:
    """The metrics the gateway's naming bar is written in, and a row per name.
    `answer` is the model's reply (a dict or JSON text); one that does not
    parse as a `marker_naming` answer scores every name unnamed and counts
    as invalid."""
    from pydantic import ValidationError

    from plexora.plugins.gating.server.autogate import answers, context, transitions

    asked = asked or packet()
    due = list(asked["evidence"]["unresolved"])
    want = expected()
    invalid = 0
    try:
        if isinstance(answer, str):
            answer = json.loads(answer)
        parsed = answers.MarkerNamingAnswer.model_validate({**answer, "kind": "marker_naming"})
        said, _refused = transitions.names_said(due, parsed)
    except (ValueError, TypeError, ValidationError):
        invalid, said = 1, {m: None for m in due}
    named = context.build([name for name, _ in CASES], ai_names=said)["named"]
    rows, tally = [], {"right": 0, "unnamed": 0, "wrong": 0}
    for name in due:
        got = (named.get(name) or {}).get("canonical")
        meant = want[name]
        verdict = "right" if got == meant else "unnamed" if got is None else "wrong"
        tally[verdict] += 1
        rows.append({"name": name, "expected": meant, "said": said.get(name), "named": got,
                     "verdict": verdict})
    n = len(due) or 1
    return {"metrics": {"naming_accuracy": round(tally["right"] / n, 4),
                        "wrong_name_rate": round(tally["wrong"] / n, 4),
                        "invalid_answer_rate": float(invalid), "asked": len(due), **tally},
            "rows": rows}


def bench_route(route: str, *, gateway=None) -> dict:
    """Ask `route` (provider/model) the bench's packet through the gateway's
    dev route and score it; returns the evaluation to submit."""
    from spatialagents.wire import ModelRequest, canonical, text_block

    from plexora.ai import tasks
    from plexora.ai.harness import prefix, route_bench, schema
    from plexora.ai.harness.gateway import GatewayClient, GatewayError

    provider, model = route_bench.split_route(route)
    gateway = gateway or GatewayClient(dev=True)
    if not gateway.dev:
        raise ValueError("The route bench runs on the dev route (an internal testing account).")
    asked = packet()
    spec = tasks.tasks()[FEATURE]
    shown = {k: v for k, v in asked.items() if not k.startswith("_")}
    request = ModelRequest(capability=spec.capability, system=prefix.gating_prefix(),
                           messages=[{"role": "user", "content": [text_block(canonical(shown))]}],
                           max_tokens=spec.max_tokens,
                           output_schema=schema.for_packet("marker_naming", FEATURE, "gating", asked),
                           context={"feature": "gating", "agent": "naming_bench", "workflow": "auto_gating",
                                    "attempt": 1},
                           model=route, task=FEATURE)
    started = time.perf_counter()
    failed, text, charged = 0, "", 0
    try:
        response = gateway.messages(request, idempotency_key=f"naming-bench.{time.time_ns()}")
        text, charged = response.text, response.charged_micro
    except GatewayError as exc:
        failed, text = 1, str(exc)
    scored = score(text if not failed else "", asked)
    measured = {**scored["metrics"], "failure_rate": float(failed),
                "cost_micro_per_image": charged, "seconds_per_image": round(time.perf_counter() - started, 1)}
    return {"feature": FEATURE, "capability": spec.capability, "provider": provider, "model": model,
            "bench_version": VERSION, "metrics": measured, "rows": scored["rows"],
            "dataset_ids": [f"naming:{len(CASES)}names"], "plexora_version": route_bench._version(),
            "images": []}

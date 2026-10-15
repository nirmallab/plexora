"""Reading an unclear expression matrix with a model, before a gating run starts.

`plexora.agent.expression` settles most matrices from their values alone: raw
intensities by their range, a matrix in the log range by its shape (raw
intensities are right-skewed whatever their scale; log1p values are not).
What it leaves open -- X is in the log range and its shape sits between the
two (`recommendation.ambiguous`) -- is put here to the gateway's cheap text
class, with the numbers: the pooled range, the shape test and a short
per-marker table (`shape.detail`), never the values themselves, plus the
matrix's name and the file's kind.

The model only reads; it decides nothing on its own. Its answer is `logged`,
`raw` or `unsure`, and only the first two become a choice -- X with log1p off
or on -- which goes to `gating_session_start` as `expression` with basis
`inferred`. The session checks it against the values again (never log1p on a
matrix whose range says logged) and applies it only where nothing was
confirmed. `unsure`, a refused call or an unreadable reply leave the matrix
unsettled: a run from the viewer then waits for the user's set-up form, and
one on a command line fails saying how to name it (`--matrix`, `--log1p`).

One call per run, before the session starts, and only for an ambiguous
matrix: most projects make none.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The harness's cheapest text class: what this call is sized as. The gateway
#: routes the task (`plexora.gating.planning`, which reads the expression
#: source); the client never names a model.
CAPABILITY = "text_routine"
AGENT = "expression_reader"
MAX_TOKENS = 600
VERDICTS = ("logged", "raw", "unsure")

SYSTEM = (
    "You read summary numbers of a single-cell expression matrix from multiplexed imaging "
    "(mean marker intensity per cell) and say whether the values were already "
    "log-transformed. Reply with one JSON object matching the schema, nothing else.\n"
    "- logged: the values are a log (log1p, log2, ln) of intensities. Typical signs: every "
    "marker's values within a few units (log1p of a 16-bit intensity is at most about 11), "
    "medians within a narrow band across markers, distributions near-symmetric or "
    "bimodal, and their log skewed left.\n"
    "- raw: intensities as measured, possibly rescaled (divided by a constant such as 65535 "
    "or 255). Typical signs: right-skewed distributions with long upper tails (p99 many "
    "times the median), near-symmetric once logged, medians spread over orders of "
    "magnitude across markers.\n"
    "- unsure: the numbers do not tell; say so rather than guess. A wrong answer either "
    "transforms the values twice or leaves raw intensities unlogged, and every gate of the "
    "run is drawn on them.\n"
    "Explain in one sentence which numbers decided it.")

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "why": {"type": "string"},
    },
    "required": ["verdict", "why"],
}


@dataclass
class Reading:
    #: `logged`, `raw`, `unsure`, or None when the call failed.
    verdict: str | None = None
    why: str | None = None
    #: The `SessionOptions.expression` it settles, or None.
    choice: dict | None = None
    #: Why nothing was read: the gateway's code, or the reply's fault.
    problem: str | None = None


def ambiguous_option(inspected) -> dict | None:
    """The matrix a model may be asked about: the one `recommend` left open
    on its shape alone (`ambiguous`), which is X. None otherwise."""
    if (inspected.get("current") or {}).get("confirmed"):
        return None
    recommendation = inspected.get("recommendation") or {}
    if recommendation.get("confidence") != "ask" or not recommendation.get("ambiguous"):
        return None
    return next((o for o in inspected.get("options") or () if o.get("value") == "X"), None)


def worth_asking(inspected) -> bool:
    return ambiguous_option(inspected) is not None


def prompt(inspected) -> str:
    import json

    option = ambiguous_option(inspected) or {}
    stats = dict(option.get("stats") or {})
    shape = dict(stats.pop("shape", None) or {})
    detail = shape.pop("detail", [])
    others = [o["value"] for o in inspected.get("options") or () if o.get("value") != "X"]
    lines = [
        f"FILE: {inspected.get('source_kind') or 'unknown'}; matrix {option.get('value', 'X')}"
        + (f"; other matrices: {', '.join(others)}" if others else ""),
        "POOLED (all markers, positive and zero values): " + json.dumps(
            {k: stats[k] for k in ("n", "min", "p50", "p99_9", "max", "integer_share",
                                   "negative_share", "markers") if k in stats}),
        "SHAPE (median over markers of skew of positive values, and of their log): " + json.dumps(
            {k: shape[k] for k in ("skew_linear", "skew_log", "markers") if k in shape}),
        "PER MARKER (positive values): " + json.dumps(detail),
        "Were these values already log-transformed?",
    ]
    return "\n".join(lines)


def settle(raw) -> Reading:
    """The model's reply as a `Reading`: a choice only for `logged` or `raw`."""
    if not isinstance(raw, dict) or raw.get("verdict") not in VERDICTS:
        return Reading(problem="the reply did not carry a verdict")
    verdict = raw["verdict"]
    why = str(raw.get("why") or "")[:300] or None
    if verdict == "unsure":
        return Reading(verdict=verdict, why=why)
    return Reading(verdict=verdict, why=why, choice={
        "features_layer": "X", "features_log": verdict == "raw", "basis": "inferred",
        "why": f"read by a model: {why or verdict}"[:400]})


def read(inspected, *, gateway, feature: str, idempotency_key: str):
    """(Reading, response or None). Never raises for the model's sake: a
    refused or unreadable call is a Reading with a `problem` and no choice."""
    from spatialagents import schema
    from spatialagents.gateway import GatewayError
    from spatialagents.wire import ModelRequest, text_block

    from plexora.ai.tasks import task_for

    request = ModelRequest(
        capability=CAPABILITY, system=[text_block(SYSTEM)],
        messages=[{"role": "user", "content": [text_block(prompt(inspected))]}],
        max_tokens=MAX_TOKENS, output_schema=schema.structured(SCHEMA),
        task=task_for(feature, "expression_setup"),
        context={"feature": feature, "agent": AGENT, "workflow": "expression"})
    try:
        response = gateway.messages(request, idempotency_key=idempotency_key)
    except GatewayError as exc:
        return Reading(problem=f"{exc.code}: {exc}"[:200]), None
    try:
        raw = response.json()
    except ValueError:
        return Reading(problem="the reply was not JSON"), response
    return settle(schema.decode(raw, SCHEMA)), response

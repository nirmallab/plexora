"""The stain-check bench: how well a model reads the Low quality staining
pictures (`qc.stain_check`, the `stain_check` packet, and its blind second
and third readers).

Real images, not synthetic ones: a failed stain on a real slide is bright
haze lighting every cell as often as it is dark, which no synthetic scene
has caught. The truth is the owner's labels (real / weak / failed) of every
marker channel on two images (`LABELS`, from the 2026-11-23 labelling of
seven): lsp35766_p193_2 (CyCIF, 8 failed stains) and imc-immucan-example
(IMC, 1). The images are the owner's, so the bench runs where they are, in
a data root that holds their projects.

Each checked channel's packet is the session's own -- the production
picture (`stain.render`), question, evidence and reading guide -- written
for any model to answer outside the gateway. The readings settle as a
session's do (`stain.settle`, best of three): a first `failed` is read again
blind, a third reading decides when two differ. Scored on the settled
verdict:

* failed_recall      owner-failed stains settled `failed`.
* real_failed_rate   owner-real stains settled `failed` -- the error that
                     would flag a working stain.
* weak_failed_rate   owner-weak stains settled `failed`, reported only: the
                     weak/failed line is the open problem, and a weak stain
                     is a low quality stain either way.
* invalid_answer_rate, failure_rate   answers that do not parse; channels
                     left without a settled verdict.

    plexora ai stain-bench --write DIR       # every channel's first reading
    plexora ai stain-bench --pending DIR     # `<question> <answer file>` still due
    plexora ai stain-bench --score DIR

Answer each `question.txt` (its picture beside it) into the answer file
`--pending` names, and run `--pending` again until it prints nothing: the
second and third readings ask the same question again, blind.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

FEATURE = "qc.stain_check"
VERSION = "stain-1"
MAX_READINGS = 3
#: The owner's labels, kept with the bench runs (not shipped in the package).
LABELS = Path(__file__).resolve().parents[2] / "docs" / "internal" / "bench" / "stain-1" / "labels.json"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "channel"


def load_labels(path=None) -> list:
    """[{image, channel, label}] -- the owner's labels the bench scores against."""
    path = Path(path or LABELS)
    if not path.exists():
        raise FileNotFoundError(f"{path} is not here: the stain bench needs the owner's labels.")
    return json.loads(path.read_text(encoding="utf-8"))


def collect(labels=None, *, session=None) -> list:
    """One item per labelled channel the stain check reads, in the current
    data root: {project, channel, label, packet, image (webp bytes)}."""
    from plexora.agent import AgentSession, presets
    from plexora.plugins.qc.server import stain

    labels = load_labels() if labels is None else labels
    by_image: dict = {}
    for row in labels:
        by_image.setdefault(row["image"], {})[row["channel"].strip()] = row["label"]
    own = session is None
    session = session or AgentSession()
    out = []
    try:
        for project, wanted in by_image.items():
            image = session.project(project).image
            names = [c.get("fullname") or c.get("name") for c in image.real_channels]
            nuclear = list(presets.nuclear_channels(names))
            for name in stain.checked_channels(names, nuclear):
                label = wanted.get(name.strip())
                if label not in ("real", "weak", "failed"):
                    continue
                try:
                    partner, words = stain.partner(names, name, exclude=nuclear)
                except Exception:  # noqa: BLE001 -- as the session: a partner is a help
                    partner, words = None, None
                rendered = stain.render(session, project, name, partner_name=partner,
                                        partner_words=words, fmt="webp", store=False)
                packet = {"kind": "stain_check",
                          "question": stain.QUESTION.format(marker=name),
                          "evidence": {**stain.evidence(names, name, project, rendered["manifest"]),
                                       "how_to_read": stain.READING},
                          "allowed": list(stain.VERDICTS)}
                out.append({"project": project, "channel": name, "label": label,
                            "packet": packet, "image": rendered["image"]})
    finally:
        if own:
            session.close()
    return out


def write(directory, labels=None, *, items=None) -> list:
    """Each channel's packet as `<project>/<channel>/question.txt` beside
    `picture.webp`, and `items.json` (no labels: the reader is blind to
    them). Returns the items written."""
    from spatialagents.wire import canonical

    from plexora.plugins.qc.server import answers as answer_models

    directory = Path(directory)
    items = collect(labels) if items is None else items
    shape = canonical(answer_models.StainCheckAnswer.model_json_schema())
    index = []
    for item in items:
        folder = directory / _safe(item["project"]) / _safe(item["channel"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "picture.webp").write_bytes(item["image"])
        (folder / "question.txt").write_text(
            "Look at the picture picture.webp in this folder, then answer this packet.\n\n"
            + canonical(item["packet"])
            + "\n\nReply with one JSON object only, of this schema:\n" + shape + "\n",
            encoding="utf-8")
        index.append({"project": item["project"], "channel": item["channel"],
                      "folder": str(folder.relative_to(directory))})
    (directory / "items.json").write_text(json.dumps({"bench_version": VERSION, "items": index},
                                                     indent=1), encoding="utf-8")
    return items


def _parse(text):
    """The verdict of one answer's text: (verdict, None) or (None, 'invalid')."""
    from pydantic import ValidationError

    from plexora.plugins.qc.server import answers as answer_models

    start, end = text.find("{"), text.rfind("}")
    try:
        said = json.loads(text[start:end + 1]) if start >= 0 < end else None
        parsed = answer_models.StainCheckAnswer.model_validate({**(said or {}), "kind": "stain_check"})
    except (ValueError, TypeError, ValidationError):
        return None, "invalid"
    return parsed.verdict, None


def readings(folder: Path) -> tuple:
    """(verdicts in order, invalid answers) of `answer_1.json` ... in `folder`;
    an invalid answer stands as no verdict and stops the count there."""
    verdicts, invalid = [], 0
    for n in range(1, MAX_READINGS + 1):
        path = folder / f"answer_{n}.json"
        if not path.exists():
            break
        verdict, problem = _parse(path.read_text(encoding="utf-8"))
        if problem:
            invalid += 1
            break
        verdicts.append(verdict)
    return verdicts, invalid


def pending(directory) -> list:
    """[(question path, answer path)] of the readings still due: a channel
    with no reading, or whose readings do not settle yet."""
    from plexora.plugins.qc.server import stain

    directory = Path(directory)
    index = json.loads((directory / "items.json").read_text(encoding="utf-8"))["items"]
    due = []
    for item in index:
        folder = directory / item["folder"]
        verdicts, invalid = readings(folder)
        if invalid or len(verdicts) >= MAX_READINGS or stain.settle(verdicts) is not None:
            continue
        due.append((folder / "question.txt", folder / f"answer_{len(verdicts) + 1}.json"))
    return due


def score(directory, labels=None) -> dict:
    """{metrics, rows}: the settled verdict of every channel `write` wrote,
    against the owner's labels."""
    from plexora.plugins.qc.server import stain

    directory = Path(directory)
    labels = load_labels() if labels is None else labels
    truth = {(r["image"], r["channel"].strip()): r["label"] for r in labels}
    index = json.loads((directory / "items.json").read_text(encoding="utf-8"))["items"]
    counts = {"real": [0, 0], "weak": [0, 0], "failed": [0, 0]}   # [settled failed, n]
    rows, invalid, unsettled, answered = [], 0, 0, 0
    for item in index:
        label = truth.get((item["project"], item["channel"].strip()))
        if label not in counts:
            continue
        verdicts, bad = readings(directory / item["folder"])
        invalid += bad
        answered += len(verdicts) + bad
        settled = None if bad else stain.settle(verdicts)
        if settled is None:
            unsettled += 1
        counts[label][1] += 1
        counts[label][0] += settled == "failed"
        if (settled == "failed") != (label == "failed") or settled is None:
            rows.append({"project": item["project"], "channel": item["channel"], "label": label,
                         "readings": verdicts, "settled": settled})

    def rate(label):
        hit, n = counts[label]
        return round(hit / n, 4) if n else None

    n = sum(c[1] for c in counts.values())
    return {"metrics": {"failed_recall": rate("failed"), "real_failed_rate": rate("real"),
                        "weak_failed_rate": rate("weak"),
                        "channels": n, "failed": counts["failed"][1], "real": counts["real"][1],
                        "weak": counts["weak"][1], "readings": answered,
                        "invalid_answer_rate": round(invalid / answered, 4) if answered else 0.0,
                        "failure_rate": round(unsettled / n, 4) if n else 0.0},
            "rows": rows}

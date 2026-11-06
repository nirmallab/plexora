"""The scripted agent of tests/test_gating_session.py, lifted without pytest
(P9): `Oracle` answers each packet from the synthetic scene's painted truth,
`drive` runs the next/answer loop. Kept verbatim apart from `info` coming
from JSON (cells and truth as make_gating_project returned them), so the
browser and CPython runs answer exactly as the product's own test does.
"""

import json

import numpy as np

from plexora.agent import invoke

#: Every packet this process was sent, by (session, packet): the reader
#: a conversation is, so a `{as_in: packet_id}` reads as the value it points at.
_HELD: dict = {}


def _read(result):
    from plexora.plugins.gating.server.autogate.packets import resolve_as_in

    for holder in (result, result.get("next") if isinstance(result.get("next"), dict)
                   else None):
        packet = (holder or {}).get("packet")
        if isinstance(packet, dict) and packet.get("session_id"):
            resolve_as_in(packet, _HELD.setdefault(packet["session_id"], {}))
    return result


def ok(result):
    assert result["ok"], json.dumps(result.get("error"), default=str)[:4000]
    out = result["result"]
    return _read(out) if isinstance(out, dict) else out


class Oracle:
    """Answers from the truth painted into the scene."""

    def __init__(self, info, style="oracle", seed=0):
        self.info = info
        self.style = style
        self.rng = np.random.default_rng(seed)
        self.index = {c["id"]: i for i, c in enumerate(info["cells"])}
        # In the table's own units: a log1p'd project's gates are log1p values.
        to_table = np.log1p if info.get("log_transformed") else (lambda v: v)
        self.values = {m: np.asarray(to_table(np.array([c[m] for c in info["cells"]],
                                                       dtype=np.float64)), dtype=np.float32)
                       for m in info["truth"]}
        self.truth = {m: np.isin([c["id"] for c in info["cells"]], ids)
                      for m, ids in info["truth"].items()}

    def errors(self, marker, low):
        called = self.values[marker] > np.float32(low)
        truth = self.truth[marker]
        return int((called & ~truth).sum()), int((~called & truth).sum())

    def best(self, marker):
        """The threshold with the fewest errors, and that error."""
        values = np.sort(self.values[marker])
        cuts = np.concatenate([[values[0] - 1], (values[:-1] + values[1:]) / 2])
        errs = [sum(self.errors(marker, c)) for c in cuts]
        i = int(np.argmin(errs))
        return float(cuts[i]), errs[i]

    def excess(self, marker, low):
        """Errors beyond the best any threshold can do."""
        return sum(self.errors(marker, low)) - self.best(marker)[1]

    def direction(self, marker, low):
        n = len(self.values[marker])
        if self.excess(marker, low) <= max(2, 0.01 * n):
            return "about_right"
        return "too_low" if low < self.best(marker)[0] else "too_high"

    def answer(self, packet):
        kind = packet["kind"]
        marker = packet["units"][0]["marker"] if packet["units"] else None
        ev = packet["evidence"]
        if kind == "t1_strip":
            verdicts = {}
            for row in ev["markers"]:
                good = self.direction(row["marker"], row["gate"]) == "about_right"
                verdicts[row["marker"]] = "ok" if good or self.style == "lazy" else "suspicious"
            return {"kind": kind, "verdicts": verdicts}
        if kind in ("t2_confirm", "t3_biological"):
            from plexora.ai.bench import rows_for

            direction = self.direction(marker, ev["candidate"]["low"])
            if self.style == "lazy":
                direction = "about_right"
            elif self.style == "noisy" and self.rng.random() < 0.3:
                direction = {"too_low": "too_high", "too_high": "too_low",
                             "about_right": "too_low"}[direction]
            return {"kind": kind, "confidence": "sure", **rows_for(direction, ev)}
        if kind == "t4_candidates":
            from plexora.ai.bench import interval_verdicts

            rows = ev["intervals"]
            if self.style == "lazy":
                return {"kind": kind, "intervals": {r["row"]: "cannot_tell" for r in rows},
                        "confidence": "sure"}
            return {"kind": kind, "confidence": "fairly_sure",
                    "intervals": interval_verdicts(self.values[marker], self.truth[marker],
                                                   rows)}
        if kind == "cell_panel":
            # Engine v2's blind panel, each cell read by its own truth. Which
            # cell is which is the stored packet's (`_panel`), never sent.
            from plexora.plugins.gating.server.autogate import engine as engines

            stored, _ = engines.store().read_packet(packet["session_id"], packet["packet_id"])
            truth = self.truth[marker]
            return {"kind": kind, "cells": {
                c["number"]: ("uncertain" if self.style == "lazy" else
                              "own_stain" if truth[self.index[c["cell_id"]]] else "not_own_stain")
                for c in stored["_panel"]["cells"]}}
        if kind == "qc_confirm":
            return {"kind": kind, "verdict": "real_signal"}
        if kind == "absence_check":
            real = bool(self.truth.get(marker, np.zeros(1, bool)).any())
            return {"kind": kind, "regions": {r: "clear" if real else "none"
                                              for r in packet["allowed"]["regions"]}}
        if kind == "regression_confirm":
            return {"kind": kind, "verdict": "holds"}
        if kind == "transfer_check":
            project = ev["this"]["project"]
            direction = self.direction(marker, ev["this"]["aligned_gate"])
            return {"kind": kind, "per_image": {project: "holds" if direction == "about_right"
                                                else direction}}
        if kind in ("panel_context", "partner_review"):
            return {"kind": kind, "entries": []}
        if kind == "display_setup":
            # The darker half of the ladder for both questions: the rule's own
            # column wherever the numbers allow it.
            rows = [r["marker"] for r in ev["rows"]]
            return {"kind": kind, "background_ok": {m: ["c1", "c2", "c3"] for m in rows},
                    "structure_ok": {m: ["c2", "c3"] for m in rows}}
        raise AssertionError(kind)


def drive(session, session_id, agent, limit=60, on_packet=None):
    """Run the next/answer loop to the end; returns the packets seen.
    `on_packet(packet, answer)` sees each exchange (P9 records them)."""
    seen = []
    result = ok(invoke(session, "gating_next", {"session_id": session_id, "wait_s": 20}))
    for _ in range(limit):
        if result["state"] != "decision":
            assert result["state"] in ("decided", "done"), result
            assert not unlooked_closes(session_id)
            return seen
        packet = result["packet"]
        seen.append(packet)
        answer = agent.answer(packet)
        if on_packet:
            on_packet(packet, answer)
        answered = ok(invoke(session, "gating_answer", {
            "session_id": session_id, "packet_id": packet["packet_id"],
            "answer": answer}))
        result = answered["next"]
    raise AssertionError("the session did not finish")


def unlooked_closes(session_id):
    """Markers a session judged (empty, failed, review) with no look behind it
    (`Engine.close`): none, ever."""
    from plexora import paths

    record = json.loads((paths.data_root() / ".agent" / "sessions" / "gating" / session_id
                         / "session.json").read_text())
    return {u["marker"]: u.get("reason") for u in record["units"].values()
            if u.get("closed_unlooked")}


def answer_review(session, started, link=None):
    """tests/autogate_fixtures.py's: the partner review answered with nothing
    to add or change."""
    sid = started["session_id"]
    for _ in range((started.get("partner_review") or {}).get("packets", 0)):
        result = invoke(session, "gating_next", {"session_id": sid, "wait_s": 20}, link=link)
        packet = (result.get("result") or {}).get("packet") or {}
        if packet.get("kind") != "partner_review":
            return
        answered = invoke(session, "gating_answer", {
            "session_id": sid, "packet_id": packet["packet_id"],
            "answer": {"kind": "partner_review", "entries": []}, "include_next": False},
            link=link)
        assert answered["ok"], answered

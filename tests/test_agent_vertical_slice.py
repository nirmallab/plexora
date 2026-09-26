"""The flagship: "Gate CD8 and visually verify it", as an agent would, over MCP.

inspect -> read the skill -> auto gate -> store it -> sample fields -> render
the checks -> adjust by a qualitative step -> receipts and audit lines. The
judgement an agent would make from the pictures is stood in for by the known
truth of the synthetic scene: the borderline group (~700) is negative, so a
gate that calls any of it positive is too low.
"""

import json

import anyio
import pytest

pytest.importorskip("mcp")

from plexora.agent import AgentSession  # noqa: E402
from plexora.mcp.server import build_server  # noqa: E402
from tests.agent_fixtures import BORDERLINE, POSITIVE, make_synthetic_project  # noqa: E402


def _json(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[-1].text)


def test_gate_cd8_and_verify_it(tmp_path):
    from mcp import Client
    from mcp.types import ImageContent

    info = make_synthetic_project(tmp_path)
    server = build_server(AgentSession())

    async def go():
        async with Client(server) as c:
            inspected = _json(await c.call_tool("inspect_project", {"project": "synth"}))
            assert inspected["table"]["n_cells"] == 64
            assert "CD8" in inspected["image"]["channels"]
            skill = await c.call_tool("read_skill", {"name": "visual-gating"})
            assert "adjust_gate" in skill.content[0].text

            auto = _json(await c.call_tool("suggest_auto_gate",
                                           {"project": "synth", "marker": "CD8"}))
            # Deliberately start low -- inside the borderline group -- so the
            # visual check has something to find.
            start = BORDERLINE * 0.9
            first = _json(await c.call_tool("set_gate", {"project": "synth", "marker": "CD8",
                                                         "low": start}))["receipt"]

            sampled = _json(await c.call_tool("sample_gate_validation_regions",
                                              {"project": "synth", "marker": "CD8",
                                               "field_size_um": 32, "classes":
                                               ["clear_negative", "clear_positive",
                                                "borderline"]}))
            assert sampled["fields"]
            check = await c.call_tool("render_gate_validation",
                                      {"project": "synth", "marker": "CD8",
                                       "field_size_um": 32, "max_fields": 4,
                                       "panel_px": 200})
            images = [part for part in check.content if isinstance(part, ImageContent)]
            report = _json(check)
            assert len(images) == len(report["fields"]) >= 1

            # The stand-in judgement: borderline cells called positive => too low.
            called_borderline = sum(
                1 for field in report["fields"] for row in field["borderline_cells"]
                if row["current_call"] == "positive")
            positives_now = report["dataset"]["positives"]
            assert positives_now > len(info["positives"])  # the gate IS too low
            receipts = [first]
            gate = start
            for _ in range(6):
                if gate > BORDERLINE * 1.2:
                    break
                adjusted = _json(await c.call_tool("adjust_gate", {
                    "project": "synth", "marker": "CD8", "direction": "up",
                    "magnitude": "large"}))
                receipts.append(adjusted["receipt"])
                gate = adjusted["receipt"]["after"]["low"]
            summary = _json(await c.call_tool("get_gated_summary",
                                              {"project": "synth", "marker": "CD8"}))
            return auto, receipts, summary, called_borderline

    auto, receipts, summary, called_borderline = anyio.run(go)
    assert auto["auto_gate"] is not None
    assert BORDERLINE * 1.2 < summary["low"] < POSITIVE * 0.85
    # Every true positive but the brightest: the stored upper bound is the
    # column's maximum and the range test is strict (`< high`), exactly as the
    # viewer's `apply_range_mask` colours it. The agent matches the viewer.
    brightest = max(c["cd8"] for c in info["cells"])
    assert summary["high"] == pytest.approx(brightest)
    assert summary["n_positive"] == len(info["positives"]) - 1
    # Every write has a receipt and an audit line, in order.
    audit = [json.loads(line) for line in
             (tmp_path / ".agent" / "audit.jsonl").read_text().splitlines()]
    assert [line["operation_id"] for line in audit] == [r["operation_id"] for r in receipts]
    assert all(line["status"] == "ok" for line in audit)
    assert all(r["source_file_modified"] is False for r in receipts)

"""tools/transcript_cost.py: one row per API call, with the call's real usage."""

import importlib.util
import json
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "transcript_cost.py"


def _module():
    spec = importlib.util.spec_from_file_location("transcript_cost", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _line(request_id, output, block):
    return {"type": "assistant", "requestId": request_id,
            "message": {"id": f"msg_{request_id}", "model": "claude-opus-5-5", "content": [block],
                        "usage": {"input_tokens": 3, "cache_read_input_tokens": 9000,
                                  "cache_creation_input_tokens": 500, "output_tokens": output}}}


def test_a_call_written_as_several_lines_counts_once_at_its_largest_output(tmp_path):
    # A subagent transcript: the same request's blocks on separate lines, the
    # output count growing as they were written. The first line alone said 12.
    path = tmp_path / "agent.jsonl"
    lines = [_line("req_1", 12, {"type": "text", "text": "thinking it over"}),
             _line("req_1", 340, {"type": "tool_use", "id": "tu_1", "name": "mcp__plexora__gating_next",
                                  "input": {}}),
             _line("req_2", 40, {"type": "text", "text": "done"})]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    calls, tools = _module().read(path, "worker")
    assert [c["output"] for c in calls] == [340, 40]
    assert all(c["cache_read"] == 9000 and c["cache_write_5m"] == 500 for c in calls)
    assert tools["gating_next"]["calls"] == 1

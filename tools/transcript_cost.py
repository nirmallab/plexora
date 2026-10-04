#!/usr/bin/env python
"""What an agent session cost: tokens per model, per tool and per context band.

    python tools/transcript_cost.py SESSION.jsonl [--prices prices.json] [--json]

Reads a Claude Code transcript (one JSON object per line) and the transcripts
of the subagents it launched (`<session>/subagents/*.jsonl` beside it), and
sums each API call's usage once (a call split over several lines shares its
`requestId`). Prints, per model: calls, uncached input, cache writes, cache
reads, output and the list-price cost; per tool: calls, result characters and
images; and the main conversation's context per call, banded, which is where
a long loop's cost hides (every call re-reads all of it).

A dev tool for measuring Plexora's AI features (docs/internal/
AUTOGATE_LIVE_RUN_2026-10-01.md). Prices are per million tokens and change:
`--prices` takes a JSON file `{model-prefix: {input, output, cache_read,
cache_write_5m, cache_write_1h}}`, matched by the longest prefix of the model id.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

#: USD per million tokens, by model-id prefix (list prices assumed 2026-10;
#: override with --prices). Cache writes: 5-minute and 1-hour TTLs.
PRICES = {
    "claude-opus": {"input": 5.0, "output": 25.0, "cache_read": 0.5,
                    "cache_write_5m": 6.25, "cache_write_1h": 10.0},
    "claude-sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3,
                      "cache_write_5m": 3.75, "cache_write_1h": 6.0},
    "claude-haiku": {"input": 1.0, "output": 5.0, "cache_read": 0.1,
                     "cache_write_5m": 1.25, "cache_write_1h": 2.0},
}

BANDS = (50_000, 100_000, 200_000, 300_000, 400_000, 600_000, 1_000_000)


def _price(model, prices):
    match = max((p for p in prices if model.startswith(p)), key=len, default=None)
    return prices.get(match) if match else None


def _lines(path):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def _short_tool(name):
    return name.split("__")[-1] if name else "?"


#: The usage numbers of one call, as a row carries them.
USAGE_KEYS = ("input", "cache_write_1h", "cache_write_5m", "cache_read", "output")


def _usage_row(message) -> dict:
    usage = message["usage"]
    creation = usage.get("cache_creation") or {}
    one_hour = int(creation.get("ephemeral_1h_input_tokens") or 0)
    written = int(usage.get("cache_creation_input_tokens") or 0)
    return {"input": int(usage.get("input_tokens") or 0), "cache_write_1h": one_hour,
            "cache_write_5m": max(0, written - one_hour),
            "cache_read": int(usage.get("cache_read_input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0)}


def read(path, label):
    """(calls, tools): one row per API call, and per-tool sums.

    A call is written as several lines, one per content block, all with its
    requestId. A subagent's transcript carries the output count as it stood
    when each block was written, so the first line under-counts it (by 21%
    over 60 transcripts, 2026-10-03): each number is the largest any of the
    call's lines gives."""
    calls, tools, by_key, names = [], defaultdict(lambda: defaultdict(int)), {}, {}
    results = set()
    for entry in _lines(path):
        message = entry.get("message") or {}
        content = message.get("content")
        if entry.get("type") == "assistant" and message.get("usage"):
            key = entry.get("requestId") or message.get("id")
            row = _usage_row(message)
            if key not in by_key:
                by_key[key] = {"who": label, "model": message.get("model") or "?", **row}
                calls.append(by_key[key])
            else:
                seen = by_key[key]
                for name in USAGE_KEYS:
                    seen[name] = max(seen[name], row[name])
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                if block.get("id") in names:
                    continue          # a line the transcript repeats
                names[block.get("id")] = _short_tool(block.get("name"))
                tools[names[block["id"]]]["calls"] += 1
            elif block.get("type") == "tool_result":
                if block.get("tool_use_id") in results:
                    continue
                results.add(block.get("tool_use_id"))
                name = names.get(block.get("tool_use_id"), "?")
                parts = block.get("content")
                parts = parts if isinstance(parts, list) else [{"type": "text",
                                                                "text": str(parts or "")}]
                for part in parts:
                    if part.get("type") == "text":
                        tools[name]["result_chars"] += len(part.get("text") or "")
                    elif part.get("type") == "image":
                        tools[name]["images"] += 1
    return calls, tools


def cost(row, prices):
    price = _price(row["model"], prices)
    if price is None:
        return None
    return sum(row[k] * price[k] for k in ("input", "output", "cache_read", "cache_write_5m",
                                           "cache_write_1h")) / 1e6


def analyse(path, prices=PRICES) -> dict:
    path = Path(path)
    calls, tools = read(path, "main")
    for sub in sorted((path.parent / path.stem / "subagents").glob("*.jsonl")):
        sub_calls, sub_tools = read(sub, sub.stem)
        meta = sub.with_suffix(".meta.json")
        if meta.exists():
            about = json.loads(meta.read_text(encoding="utf-8"))
            for call in sub_calls:
                call["who"] = f"{about.get('agentType', 'agent')}:{sub.stem[-6:]}"
        calls += sub_calls
        for name, sums in sub_tools.items():
            for key, value in sums.items():
                tools[name][key] += value
    models = defaultdict(lambda: defaultdict(float))
    for call in calls:
        row = models[call["model"]]
        row["calls"] += 1
        for key in ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output"):
            row[key] += call[key]
        row["usd"] += cost(call, prices) or 0.0
    bands = defaultdict(lambda: defaultdict(float))
    for call in calls:
        if call["who"] != "main":
            continue
        context = call["input"] + call["cache_read"] + call["cache_write_5m"] + \
            call["cache_write_1h"]
        band = next((b for b in BANDS if context <= b), BANDS[-1])
        bands[band]["calls"] += 1
        bands[band]["cache_read"] += call["cache_read"]
        bands[band]["usd"] += cost(call, prices) or 0.0
    return {"models": {m: dict(v) for m, v in models.items()},
            "tools": {t: dict(v) for t, v in sorted(tools.items(),
                                                    key=lambda kv: -kv[1]["result_chars"])},
            "context_bands": {f"<= {b // 1000}k": dict(v) for b, v in sorted(bands.items())},
            "workers": sorted({c["who"] for c in calls} - {"main"}),
            "unpriced": sorted({c["model"] for c in calls if _price(c["model"], prices) is None
                                and c["model"] != "<synthetic>"}),
            "usd": sum(v["usd"] for v in models.values())}


def _table(rows, columns):
    lines = ["| " + " | ".join(["", *columns]) + " |",
             "|" + "---|" * (len(columns) + 1)]
    for name, row in rows.items():
        cells = [f"{row.get(c, 0):,.2f}" if c == "usd" else f"{int(row.get(c, 0)):,}"
                 for c in columns]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("transcript", help="The session's .jsonl transcript.")
    parser.add_argument("--prices", help="JSON file of prices per million tokens.")
    parser.add_argument("--json", action="store_true", help="Print the raw numbers.")
    args = parser.parse_args(argv)
    prices = json.loads(Path(args.prices).read_text(encoding="utf-8")) if args.prices \
        else PRICES
    result = analyse(args.transcript, prices)
    if args.json:
        print(json.dumps(result, indent=1))
        return 0
    print("## Per model\n")
    print(_table(result["models"], ("calls", "input", "cache_write_5m", "cache_write_1h",
                                    "cache_read", "output", "usd")))
    print(f"\nTotal: ${result['usd']:,.2f} at the prices given; "
          f"{len(result['workers'])} worker conversation(s).")
    if result["unpriced"]:
        print(f"No price for {', '.join(result['unpriced'])}: counted at $0 (--prices).")
    print()
    print("## Main conversation, by context size per call\n")
    print(_table(result["context_bands"], ("calls", "cache_read", "usd")))
    print("\n## Per tool\n")
    print(_table(dict(list(result["tools"].items())[:25]), ("calls", "result_chars",
                                                            "images")))
    return 0


if __name__ == "__main__":
    sys.exit(main())

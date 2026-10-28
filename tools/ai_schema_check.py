#!/usr/bin/env python
"""Whether the provider compiles each answer schema Plexora AI can send.

    python tools/ai_schema_check.py [PATTERN ...] [--dev]

Sends every schema `plexora.ai.harness.schema.sendable()` lists (or those
whose label contains a PATTERN, e.g. `artifact_confirm`) once through the
gateway, with a one-line prompt and a few output tokens, and prints each
one's optional and union counts and whether it was accepted. A schema the
provider will not compile is refused before any model runs ("Schema is too
complex", not charged); an accepted one costs a fraction of a credit.

A dev tool, for the test gate on `schema.VERIFIED`: a schema past the
envelope it records is refused by the tests until this has shown the
provider compiles it. The client's own words fallback is bypassed, so a
refusal is reported, not answered around. Uses this device's licence (or
`BIOCOGNIA_AI_TOKEN`); `--dev` the dev route.
"""

from __future__ import annotations

import argparse
import sys
import time


def _task_of(label: str) -> tuple[str, str]:
    """(workflow, task) a label's schema is sent under: its task, or the
    task its kind's packets belong to (`tasks.task_for`)."""
    from plexora.ai import tasks

    workflow, rest = label.split(":", 1)
    if rest.startswith("task:"):
        return workflow, rest.split(":", 1)[1]
    kind = rest.split(":", 1)[0]
    return workflow, tasks.task_for(workflow, kind) or f"{workflow}.default"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("patterns", nargs="*", help="only labels containing one of these")
    parser.add_argument("--dev", action="store_true", help="the gateway's dev route")
    args = parser.parse_args(argv)

    from spatialagents import gateway as agent_gateway
    from spatialagents.wire import ModelRequest

    from plexora.ai.harness import schema
    from plexora.ai.harness.gateway import GatewayClient, GatewayError

    client = GatewayClient(dev=args.dev or None)
    agents = {"gating": ("gating_worker", "auto_gating"), "qc": ("qc_worker", "auto_qc")}
    refused = 0
    for label, sent in schema.sendable():
        if args.patterns and not any(p in label for p in args.patterns):
            continue
        workflow, task = _task_of(label)
        agent, flow = agents[workflow]
        request = ModelRequest(
            capability="vision_judgement",
            system=[{"type": "text", "text": "A schema check: answer with the least committal "
                                             "values the schema allows."}],
            messages=[{"role": "user", "content": [{"type": "text", "text": "Answer."}]}],
            max_tokens=32, output_schema=sent,
            context={"feature": workflow, "agent": agent, "workflow": flow}, task=task)
        counts = schema.complexity(sent)
        optional, unions = counts["optional"], counts["unions"]
        try:
            # The base class's call: Plexora's would answer a refusal in words.
            response = agent_gateway.GatewayClient.messages(
                client, request, idempotency_key=f"schemacheck.{int(time.time() * 1000)}")
            verdict = f"accepted ({response.model}, {response.charged_micro / 10_000:.2f} credits)"
        except GatewayError as exc:
            refused += 1
            verdict = f"REFUSED {exc.code}: {str(exc)[:160]}"
        print(f"{label:46s} optional {optional:3d}  unions {unions:3d}  {verdict}", flush=True)
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())

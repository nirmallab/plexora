"""`plexora ai run | trace | credits`: the harness from the command line."""

from __future__ import annotations

import json
import os
import sys


def _credits(micro) -> str:
    micro = int(micro or 0)
    return f"{micro / 10_000:,.1f} credits (${micro / 1_000_000:,.2f})"


def _client(args):
    from plexora.ai.harness.gateway import GatewayClient

    if getattr(args, "dev", False):
        os.environ["PLEXORA_AI_DEV"] = "1"
    return GatewayClient(getattr(args, "gateway", None) or None,
                         dev=True if getattr(args, "dev", False) else None)


def _progress(event: dict) -> None:
    kind = event.get("event")
    if kind == "started":
        print(f"  session {event['session_id']}: {event.get('units')} markers", file=sys.stderr)
    elif kind == "quoted":
        print(f"  quoted: at most {event.get('quote_credits')} credits", file=sys.stderr)
    elif kind == "answered":
        mark = "" if event.get("valid") else "  (invalid)"
        print(f"  {event.get('packet_id')} {event.get('kind')} {','.join(event.get('markers') or [])}"
              f"  worker {event.get('worker')}{mark}", file=sys.stderr)
    elif kind == "paused":
        print(f"  paused: {event.get('reason')}", file=sys.stderr)
    elif kind == "waiting_for_user":
        print("  the session is waiting for a person's answer in the viewer", file=sys.stderr)


def run_command(args) -> int:
    from plexora.ai.harness.decision import GatingOptions, GatingRun, run_many
    from plexora.ai.harness.gateway import GatewayError

    if args.model and not args.dev:
        print("--model is accepted only with --dev.", file=sys.stderr)
        return 2
    options = GatingOptions(
        project=args.projects[0], mode=args.mode, capability=args.capability, model=args.model,
        markers=[m.strip() for m in args.markers.split(",") if m.strip()] if args.markers else None,
        units_per_worker=max(1, args.units_per_worker), declare_run=args.declare_run,
        resume_session=args.resume_session)
    try:
        gateway = _client(args)
        if len(args.projects) > 1:
            if args.resume_session:
                print("--resume takes one project.", file=sys.stderr)
                return 2
            summary = run_many(args.projects, options, gateway=gateway, parallel=args.parallel,
                               on_event=_progress)
            ok = not summary["failed"]
        else:
            summary = GatingRun(options, gateway=gateway, on_event=_progress).run()
            ok = summary["status"] in ("done", "paused", "waiting_for_user")
    except GatewayError as exc:
        print(f"Plexora AI: {exc}", file=sys.stderr)
        return 1
    if args.run_json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print_summary(summary)
    return 0 if ok else 1


def _print_summary(summary: dict) -> None:
    if "sessions" in summary:
        print(f"run {summary['run_id']}: {summary['done']} done, {summary['failed']} failed, "
              f"peak {summary['peak_parallel']} in parallel, {_credits(summary['charged_micro'])}")
        for task, s in summary["sessions"].items():
            if s:
                print(f"  {task}: {s['status']}, {s['packets']} packets, {_credits(s['charged_micro'])}")
        return
    print(f"run {summary['run_id']} ({summary['project']}): {summary['status']}"
          + (f" -- {summary['reason']}" if summary.get("reason") else ""))
    print(f"  session {summary['session_id']}: {summary['packets']} packets, {summary['model_calls']} "
          f"model calls, {summary['workers']} workers, {summary['invalid_answers']} invalid answers")
    cache = summary["cache"]
    print(f"  cache: {cache['read_share']:.0%} of input read from cache; verdicts {cache['verdicts']}")
    print(f"  charged: {_credits(summary['charged_micro'])}")
    if summary["status"] == "paused":
        print(f"  resume with: plexora ai run gating {summary['project']} --resume {summary['session_id']}")


def trace_command(args) -> int:
    from plexora.ai.harness.trace import TraceStore

    store = TraceStore()
    if not args.trace_run:
        rows = store.runs()
        if args.trace_json:
            print(json.dumps(rows, indent=2, default=str))
            return 0
        if not rows:
            print("No harness runs yet.")
        for r in rows:
            print(f"{r['run_id']}  {r['kind']:<12} {r['status']:<16} {r['project'] or '':<20} "
                  f"{r['session_id'] or ''}")
        return 0
    run = store.run(args.trace_run)
    if not run:
        print(f"No run {args.trace_run!r}.", file=sys.stderr)
        return 1
    if args.cache:
        report = store.cache_report(run["run_id"])
        if args.trace_json:
            print(json.dumps(report, indent=2))
        else:
            print(f"{run['run_id']}: {report['calls']} calls over {report['workers']} workers")
            print(f"  cache-read share {report['cache_read_share']:.1%}; verdicts {report['verdicts']}")
            print(f"  prefixes {report['prefixes']} (more than one means the cached prefix changed)")
            print(f"  tokens in {report['input_tokens']:,}, out {report['output_tokens']:,}; "
                  f"{_credits(report['charged_micro'])}; invalid answers {report['invalid_answers']}")
        return 0
    calls = store.calls(run["run_id"])
    out = {**run, "summary": json.loads(run["summary_json"] or "null"), "calls": calls,
           "tasks": store.tasks(run["run_id"])}
    out.pop("summary_json", None)
    if args.trace_json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    print(f"{run['run_id']}  {run['kind']}  {run['status']}  session {run['session_id']}")
    for c in calls:
        print(f"  w{c['worker']:<3} {c['packet_id'] or '':<8} {c['kind'] or '':<20} {c['verdict']:<5} "
              f"read {c['cache_read']:>7,} write {c['cache_write']:>7,} out {c['output_tokens']:>5,} "
              f"{(c['charged_micro'] or 0) / 10_000:>7.2f} cr")
    return 0


def credits_command(args) -> int:
    from plexora.ai.harness.gateway import GatewayError

    try:
        gateway = _client(args)
        balance = gateway.balance()
        usage = gateway.usage(args.days)
    except GatewayError as exc:
        print(f"Plexora AI: {exc}", file=sys.stderr)
        return 1
    print(f"Account {balance.get('account_id')} ({balance.get('mode')}): "
          f"{_credits(balance.get('available_micro'))} available")
    if balance.get("allowance_micro"):
        print(f"  of which this month's allowance: {_credits(balance['allowance_micro'])}")
    if balance.get("held_micro"):
        print(f"  reserved by running work: {_credits(balance['held_micro'])}")
    rows = usage.get("rows") or []
    if rows:
        print(f"Last {usage.get('days')} days:")
        for r in rows:
            print(f"  {r['day']}  {r['feature'] or '-':<10} {r['billing']:<7} {r['calls']:>5} calls  "
                  f"{_credits(r['charged_micro'])}")
    return 0

"""`plexora ai run gating|qc | trace | credits | chat | route-bench`: the harness from the command line."""

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
        print(f"  session {event['session_id']}: {event.get('units')} {event.get('unit_noun') or 'unit'}s",
              file=sys.stderr)
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


def _names(value):
    return [m.strip() for m in value.split(",") if m.strip()] if value else None


def _apply_qc_limits(args) -> None:
    """`--on-limit` / `--max-extensions` are the QC session's defaults too."""
    from plexora.plugins.qc.server.schemas import LIMIT_ENV

    if getattr(args, "gating_on_limit", None):
        os.environ[LIMIT_ENV["on_limit"]] = args.gating_on_limit
    if getattr(args, "gating_max_extensions", None) is not None:
        os.environ[LIMIT_ENV["max_extensions"]] = str(args.gating_max_extensions)


def run_command(args) -> int:
    from plexora.ai.harness.decision import RUNS, GatingOptions, QCOptions, run_many
    from plexora.ai.harness.gateway import GatewayError

    if args.model and not args.dev:
        print("--model is accepted only with --dev.", file=sys.stderr)
        return 2
    target = getattr(args, "run_target", "gating")
    common = dict(project=args.projects[0], mode=args.mode, capability=args.capability, model=args.model,
                  declare_run=args.declare_run, resume_session=args.resume_session)
    if args.units_per_worker:
        common["units_per_worker"] = max(1, args.units_per_worker)
    if target == "qc":
        _apply_qc_limits(args)
        options = QCOptions(channels=_names(getattr(args, "channels", None)), **common)
    else:
        options = GatingOptions(markers=_names(args.markers), **common,
                                parallel_markers=max(1, int(getattr(args, "parallel_markers", 1) or 1)))
    try:
        gateway = _client(args)
        if len(args.projects) > 1:
            if args.resume_session:
                print("--resume takes one project.", file=sys.stderr)
                return 2
            summary = run_many(args.projects, options, gateway=gateway, parallel=args.parallel,
                               on_event=_progress, workflow=target)
            ok = not summary["failed"]
        else:
            summary = RUNS[target](options, gateway=gateway, on_event=_progress).run()
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
    if summary.get("parallel_markers"):
        print(f"  parallel markers: {summary['parallel_markers']} lanes, at most "
              f"{summary['peak_outstanding']} packets out at once, {summary['reissued']} reissued")
    cache = summary["cache"]
    print(f"  cache: {cache['read_share']:.0%} of input read from cache; verdicts {cache['verdicts']}")
    print(f"  charged: {_credits(summary['charged_micro'])}")
    if summary["status"] == "paused":
        print(f"  resume with: plexora ai run {summary.get('workflow', 'gating')} {summary['project']} "
              f"--resume {summary['session_id']}")


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
        print(f"  w{c['worker']:<3} {c['packet_id'] or '':<8} {c['kind'] or '':<20} {c['verdict']:<8} "
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


def route_bench_command(args) -> int:
    from plexora.ai import bench_data
    from plexora.ai.harness import route_bench
    from plexora.ai.harness.gateway import GatewayClient, GatewayError

    scenarios = (list(bench_data.SCENARIOS) if args.synthetic == "all"
                 else [s.strip() for s in args.synthetic.split(",") if s.strip()])
    unknown = [s for s in scenarios if s not in bench_data.SCENARIOS]
    if unknown:
        print(f"Unknown scenario(s): {', '.join(unknown)}.", file=sys.stderr)
        return 2
    token = (os.environ.get("PLEXORA_ADMIN_TOKEN") or "").strip()
    if args.submit and not token:
        print("--submit needs PLEXORA_ADMIN_TOKEN (the licence service's admin token).", file=sys.stderr)
        return 2
    try:
        evaluation = route_bench.bench_route(
            args.route, feature=args.feature, capability=args.capability, scenarios=scenarios,
            markers=[m.strip() for m in args.markers.split(",") if m.strip()] if args.markers else None,
            gateway=GatewayClient(args.gateway or None, dev=True), seed=args.seed, grid=args.grid,
            size=args.size, on_event=_progress)
    except (ValueError, GatewayError) as exc:
        print(f"Plexora AI: {exc}", file=sys.stderr)
        return 1
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(evaluation, handle, indent=2, default=str)
    m = evaluation["metrics"]
    print(f"{args.route} for {args.feature}/{args.capability} on {len(scenarios)} image(s):")
    print(f"  code agreement {m['code_agreement']:.3f}, marker F1 {m['marker_f1']:.3f}, "
          f"invalid answers {m['invalid_answer_rate']:.1%}, failures {m['failure_rate']:.1%}, "
          f"cache reads {m['cache_hit_ratio']:.0%}")
    print(f"  {m['model_calls']} calls, {_credits(m['cost_micro_per_image'])} per image at cost, "
          f"{m['seconds_per_image']} s per image")
    if not args.submit:
        return 0
    try:
        verdict = route_bench.submit(evaluation, admin_token=token, base_url=args.gateway or None)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if verdict.get("passed"):
        print(f"  evaluation {verdict['id']} PASSED: publish with evaluation_id {verdict['id']}")
        return 0
    print(f"  evaluation {verdict.get('id')} did not pass: {'; '.join(verdict.get('misses') or [])}")
    return 1


def chat_command(args, *, read=input, write=print) -> int:
    """`plexora ai chat [--resume ID]`: a conversation with Plexora AI in the terminal."""
    from plexora.agent import registry
    from plexora.agent.errors import AgentError
    from plexora.agent.policy import Policy
    from plexora.ai.harness.approvals import decide
    from plexora.ai.harness.conversations import ChatService
    from plexora.ai.harness.gateway import GatewayError

    import contextlib

    with contextlib.redirect_stdout(sys.stderr):
        registry.discover(None)
    if getattr(args, "model", None) and not getattr(args, "dev", False):
        print("--model is accepted only with --dev.", file=sys.stderr)
        return 2
    options = {"model": args.model} if getattr(args, "model", None) else {}
    service = ChatService(gateway_factory=lambda: _client(args), runner_options=options)
    try:
        if args.resume:
            described = service.describe(args.resume)
            conversation_id = args.resume
        else:
            described = service.start(Policy(), title="terminal")
            conversation_id = described["conversation_id"]
        runner = service.runner(conversation_id)
    except (AgentError, GatewayError) as exc:
        print(f"Plexora AI: {exc}", file=sys.stderr)
        return 1
    write(f"Plexora AI conversation {conversation_id} -- {described.get('disclosure')}")
    write("Type a message; an empty line or 'exit' ends.  Resume later with: "
          f"plexora ai chat --resume {conversation_id}")
    while True:
        try:
            text = read("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text or text.lower() in ("exit", "quit"):
            break
        streaming = False
        for event in runner.turn(text):
            kind = event.get("event")
            who = f"[{event['agent']}] " if event.get("agent") else ""
            if kind == "text_delta" and not event.get("agent"):
                if not streaming:
                    sys.stdout.write("plexora> ")
                    streaming = True
                sys.stdout.write(event["text"])
                sys.stdout.flush()
                continue
            if streaming and kind != "text_delta":
                sys.stdout.write("\n")
                streaming = False
            if kind == "tool_call":
                write(f"  {who}-> {event['tool']}")
            elif kind == "tool_result":
                extra = f"  (undo: {event['operation_id']})" if event.get("undo") else ""
                write(f"  {who}<- {event['tool']}: {'ok' if event['ok'] else 'error'} [{event['source']}]{extra}")
            elif kind == "approval_requested":
                write(f"  {who}{event['tool']} ({event['permission']}) wants to run with "
                      f"{json.dumps(event.get('arguments'))}")
                answer = read("  approve? [y/N] ").strip().lower()
                decide(service.store, conversation_id, event["approval_id"], answer in ("y", "yes"), by="terminal")
            elif kind == "usage" and not event.get("agent"):
                write(f"  ({_credits(event.get('total_charged_micro'))} so far)")
            elif kind == "paused":
                write(f"  paused: {event.get('reason')} -- {event.get('resume')}")
            elif kind == "error":
                write(f"  error: {event.get('code')}: {event.get('message')}")
            elif kind in ("agent_started", "agent_finished"):
                write(f"  {who}{kind.replace('_', ' ')}: {event.get('brief') or event.get('summary') or ''}")
    service.store.release(conversation_id)
    return 0

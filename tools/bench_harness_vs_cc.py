#!/usr/bin/env python
"""Plexora's built-in harness vs Claude Code over MCP: one gating task, both arms.

    python tools/bench_harness_vs_cc.py preflight
    python tools/bench_harness_vs_cc.py snapshot
    python tools/bench_harness_vs_cc.py run H1        # H* harness, C* Claude Code
    python tools/bench_harness_vs_cc.py collect
    python tools/bench_harness_vs_cc.py report

The fixed spec is docs/internal/bench/harness_vs_cc_lsp11385.yaml; runs land in
docs/internal/bench/runs/<campaign>/<run>/ (`--campaign`, default today's date).
Both arms' model calls pass through tools/bench_proxy.py, which records every
request body, image, cache mark, usage line and timing: the one instrument
the comparison's numbers come from.

  harness  plexora.ai.harness.decision RUNS["gating"] in a child process, its
           gateway (BIOCOGNIA_AI_GATEWAY) the proxy in front of the production
           gateway, whose per-task routes the user has set to the spec's models
  claude   headless `claude -p` from a clean scratch directory holding only the
           gating worker's agent file, ANTHROPIC_BASE_URL the proxy, user
           settings/CLAUDE.md/skills left out (`--setting-sources project`), the
           Plexora MCP server the only one; the session's delegate block names the
           worker models from the user's ai-models.yaml

Every session runs in propose mode with reuse_answers off and its own agent
label, so no run writes a gate or replays another's answers; the project's gate
state is hashed before and after each run. Measurement only: nothing here
changes how either arm works.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs/internal/bench/harness_vs_cc_lsp11385.yaml"
RUNS_DIR = ROOT / "docs/internal/bench/runs"
ANTHROPIC = "https://api.anthropic.com"
#: Session variables of an enclosing Claude Code that must not reach the child.
CLAUDE_ENV_DROP = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
                   "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CODE_MESSAGING_SOCKET",
                   "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_EXECPATH",
                   "CLAUDE_CODE_EMIT_STARTUP_TIMING", "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
                   "CLAUDE_CODE_QUESTION_PREVIEW_FORMAT", "CLAUDE_CODE_ENABLE_TASKS", "ANTHROPIC_BASE_URL")


# -- spec and places --------------------------------------------------------------------


def spec() -> dict:
    import yaml

    return yaml.safe_load(SPEC.read_text())


def data_root() -> Path:
    from plexora import paths

    return paths.settings_path().parent


def campaign_dir(args) -> Path:
    return RUNS_DIR / args.campaign


def project_db(s) -> Path:
    return data_root() / s["project"] / f"{s['project']}.db"


def gate_revision(s) -> str:
    """sha1 of the project's stored gates (plugin_gating_state.cells)."""
    with sqlite3.connect(f"file:{project_db(s)}?mode=ro", uri=True) as db:
        rows = db.execute("select datasource, cells, is_deleted from plugin_gating_state order by id").fetchall()
    digest = hashlib.sha1()
    for source, cells, deleted in rows:
        digest.update(str(source).encode() + bytes(cells or b"") + str(deleted).encode())
    return digest.hexdigest()


def code_state() -> dict:
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    diff = subprocess.run(["git", "diff", "HEAD"], cwd=ROOT, capture_output=True).stdout
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "plexora", ".claude"],
                               cwd=ROOT, capture_output=True, text=True).stdout.split()
    digest = hashlib.sha1(diff)
    for name in sorted(untracked):
        path = ROOT / name
        if path.is_file():
            digest.update(name.encode() + path.read_bytes())
    return {"git_sha": sha, "dirty": bool(diff or untracked), "dirty_hash": digest.hexdigest()[:16],
            "untracked": untracked}


def _json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str))


# -- proxy ------------------------------------------------------------------------------


class Proxy:
    def __init__(self, upstream: str, out: Path):
        out.mkdir(parents=True, exist_ok=True)
        self.log = open(out.parent / f"{out.name}.log", "w")
        self.process = subprocess.Popen([sys.executable, str(ROOT / "tools/bench_proxy.py"), "--upstream", upstream,
                                         "--out", str(out)], stdout=subprocess.PIPE, stderr=self.log, text=True)
        line = self.process.stdout.readline()
        if not line.startswith("PORT"):
            raise RuntimeError(f"proxy did not start: {line!r}")
        self.url = f"http://127.0.0.1:{int(line.split()[1])}"

    def close(self) -> None:
        self.process.send_signal(signal.SIGINT)
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.log.close()


def claude_binary() -> str:
    return os.environ.get("BENCH_CLAUDE") or shutil.which("claude") or "claude"


def claude_version() -> str:
    out = subprocess.run([claude_binary(), "--version"], capture_output=True, text=True).stdout
    return out.split()[0] if out.strip() else ""


def _version_tuple(text):
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


# -- preflight / snapshot ----------------------------------------------------------------


def preflight(args) -> int:
    s = spec()
    problems: list = []
    from plexora.ai import models_config

    loaded = models_config.load()
    mapping = {t: models_config.model_for(f"gating.{t}") for t in s["models"]}
    print(f"models file: {loaded['path']}")
    for task, model in s["models"].items():
        alias = mapping.get(task)
        resolved = s["aliases"].get(alias, alias)
        ok = _same_model(resolved, model)
        print(f"  gating.{task:22s} file={alias!s:8s} spec={model:20s} {'ok' if ok else 'MISMATCH'}")
        if not ok:
            problems.append(f"gating.{task}: models file says {alias}, spec says {model}")
    problems += [f"models file: {p}" for p in loaded.get("problems") or ()]
    db = project_db(s)
    print(f"project db: {db} ({'present' if db.exists() else 'MISSING'})")
    if not db.exists():
        problems.append("project db missing")
    else:
        print(f"gate revision: {gate_revision(s)}")
    state = code_state()
    print(f"code: {state['git_sha'][:10]} dirty={state['dirty']} hash={state['dirty_hash']}")
    version = claude_version()
    print(f"claude: {claude_binary()} {version}")
    if _version_tuple(version or "0") < _version_tuple(str(s.get("claude_min_version", "0"))):
        problems.append(f"Claude Code {version} is older than {s['claude_min_version']} (set BENCH_CLAUDE)")
    try:
        from plexora.ai.harness.gateway import GatewayClient

        balance = GatewayClient().balance()
        print(f"gateway balance: {json.dumps(balance)[:200]}")
    except Exception as exc:              # noqa: BLE001
        problems.append(f"gateway: {exc}")
    print(f"routes the production gateway must serve (/admin/ai), effort unset (= {s['effort']}):")
    for task, model in s["models"].items():
        print(f"  gating.{task:22s} -> {model}")
    for p in problems:
        print(f"PROBLEM: {p}")
    return 1 if problems else 0


SNAP_PARTS = ("{project}/{project}.db", ".agent/memo/gating/{project}.jsonl", ".agent/gating/panels")


def snapshot(args) -> int:
    s = spec()
    out = campaign_dir(args) / "snapshot"
    if out.exists() and not args.force:
        print(f"{out} exists (--force to replace)")
        return 1
    root = data_root()
    for part in SNAP_PARTS:
        src = root / part.format(project=s["project"])
        dst = out / part.format(project=s["project"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif src.exists():
            shutil.copy2(src, dst)
    _json(out / "snapshot.json", {"taken": time.time(), "gate_revision": gate_revision(s), **code_state()})
    print(f"snapshot -> {out}; gate revision {gate_revision(s)}")
    return 0


def restore(args) -> int:
    """The project's files as the snapshot holds them."""
    s = spec()
    src_root = campaign_dir(args) / "snapshot"
    if not (src_root / "snapshot.json").exists():
        raise SystemExit(f"no snapshot in {src_root}")
    root = data_root()
    for part in SNAP_PARTS:
        src = src_root / part.format(project=s["project"])
        dst = root / part.format(project=s["project"])
        if src.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
        elif src.exists():
            shutil.copy2(src, dst)
    print(f"restored; gate revision {gate_revision(s)}")
    return 0


def clean(args) -> str:
    """The snapshot back, then every gate reset and every reusable answer or
    cache deleted. Returns the clean gate revision."""
    restore(args)
    s = spec()
    root = data_root()
    from plexora.agent import AgentSession, registry

    registry.discover(["gating"])
    session = AgentSession(table_limit=4)
    result = registry.invoke(session, "gating.reset", {"project": s["project"], "include_approved": True,
                                                             "clear_provenance": True})
    if not result.get("ok"):
        raise SystemExit(f"reset_gates failed: {result.get('error')}")
    removed = []
    for part in (f".agent/memo/gating/{s['project']}.jsonl", ".agent/gating/panels", ".agent/ai/toolcache"):
        path = root / part
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(part)
        elif path.exists():
            path.unlink()
            removed.append(part)
    revision = gate_revision(s)
    reset = (result.get("result") or {})
    print(f"clean: reset {len(reset.get('reset') or ())} gates, skipped {len(reset.get('skipped') or ())}; "
          f"removed {removed}; gate revision {revision}")
    return revision


def _check_start(args, s) -> str:
    snap = campaign_dir(args) / "snapshot" / "snapshot.json"
    if not snap.exists():
        raise SystemExit("take a snapshot first")
    want = json.loads(snap.read_text())["gate_revision"]
    have = gate_revision(s)
    if have != want:
        raise SystemExit(f"gate revision {have} != snapshot {want}: restore first")
    return have


# -- the arms -----------------------------------------------------------------------------


HARNESS_CHILD = r"""
import json, sys, time
from plexora.ai.harness.decision import RUNS, GatingOptions
from plexora.ai.harness.gateway import GatewayClient
cfg = json.loads(sys.argv[1])
events = open(cfg["events"], "a")
def on_event(e):
    events.write(json.dumps({"t": time.time(), **e}, default=str) + "\n"); events.flush()
options = GatingOptions(project=cfg["project"], mode=cfg["mode"], markers=cfg["markers"],
                        context=cfg["context"], start_options=cfg["start_options"],
                        parallel_markers=int(cfg.get("parallel_markers") or 1))
summary = RUNS["gating"](options, gateway=GatewayClient(cfg["gateway"]), on_event=on_event).run()
print(json.dumps(summary, default=str))
"""


def session_options(s, label) -> dict:
    o = s["session"]
    return {"qc": o["qc"], "reuse_answers": o["reuse_answers"], "seed": o["seed"], "on_limit": o["on_limit"],
            "agent": label}


def run_harness(args, s, run: str, out: Path, label: str) -> dict:
    from plexora.ai.harness.gateway import gateway_url

    upstream = gateway_url()
    proxy = Proxy(upstream, out / "proxy")
    cfg = {"project": s["project"], "mode": s["session"]["mode"], "markers": s["markers"],
           "context": s["context_note"], "start_options": session_options(s, label),
           "parallel_markers": int(s.get("parallel_markers") or 1),
           "gateway": proxy.url, "events": str(out / "events.jsonl")}
    _json(out / "input.json", {**cfg, "upstream": upstream})
    started = time.time()
    try:
        result = subprocess.run([sys.executable, "-c", HARNESS_CHILD, json.dumps(cfg)], cwd=ROOT,
                                capture_output=True, text=True)
    finally:
        proxy.close()
    ended = time.time()
    (out / "stderr.log").write_text(result.stderr)
    summary = None
    for line in reversed(result.stdout.strip().splitlines()):
        try:
            summary = json.loads(line)
            break
        except ValueError:
            continue
    _json(out / "summary.json", summary)
    return {"started": started, "ended": ended, "exit": result.returncode,
            "session_id": (summary or {}).get("session_id"), "harness_run_id": (summary or {}).get("run_id")}


def claude_prompt(s, label) -> str:
    o = session_options(s, label)
    return (
        f"Gate these markers of the Plexora project {s['project']}: {', '.join(s['markers'])}.\n\n"
        f"About the sample, from the user: {s['context_note']}\n\n"
        "Start the gating session with exactly these options: "
        f"project={s['project']}, markers={json.dumps(s['markers'])}, mode={s['session']['mode']}, "
        f"qc={o['qc']}, reuse_answers={str(o['reuse_answers']).lower()}, seed={o['seed']}, "
        f"on_limit={o['on_limit']}, agent={label!r}. "
        "Do not commit anything: when every marker is decided, finish the session with "
        "action=close, and reply with one line per marker: its state and gate.")


def run_claude(args, s, run: str, out: Path, label: str) -> dict:
    cwd = out / "cwd"
    (cwd / ".claude/agents").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / ".claude/agents/plexora-gating-worker.md", cwd / ".claude/agents/")
    serve = ["-m", "plexora", "mcp", "serve"]
    if s.get("mcp_profile", "full") != "full":
        serve += ["--profile", s["mcp_profile"]]
    mcp = {"mcpServers": {"plexora": {"command": sys.executable, "args": serve, "env": {}}}}
    _json(out / "mcp.json", mcp)
    proxy = Proxy(ANTHROPIC, out / "proxy")
    prompt = claude_prompt(s, label)
    command = [claude_binary(), "-p", prompt, "--model", s["coordinator_model"], "--effort", s["effort"],
               "--output-format", "stream-json", "--verbose", "--strict-mcp-config",
               "--mcp-config", str(out / "mcp.json"), "--setting-sources", "project",
               "--disable-slash-commands", "--allowedTools", "mcp__plexora__*", "Agent"]
    env = {k: v for k, v in os.environ.items() if k not in CLAUDE_ENV_DROP}
    env["ANTHROPIC_BASE_URL"] = proxy.url
    for alias, model in s["aliases"].items():
        env[f"ANTHROPIC_DEFAULT_{alias.upper()}_MODEL"] = model
    env["PYTHONPATH"] = str(ROOT)
    _json(out / "input.json", {"prompt": prompt, "command": command[:2] + ["<prompt>"] + command[3:],
                               "claude_version": claude_version(),
                               "alias_env": {k: v for k, v in env.items() if k.startswith("ANTHROPIC_DEFAULT_")},
                               "cwd": str(cwd), "mcp": mcp})
    started = time.time()
    try:
        with open(out / "stream.jsonl", "w") as stream, open(out / "stderr.log", "w") as err:
            code = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=err,
                                  stdin=subprocess.DEVNULL).returncode
    finally:
        proxy.close()
    ended = time.time()
    lines = [json.loads(x) for x in (out / "stream.jsonl").read_text().splitlines() if x.strip().startswith("{")]
    result = next((x for x in reversed(lines) if x.get("type") == "result"), {})
    init = next((x for x in lines if x.get("type") == "system" and x.get("subtype") == "init"), {})
    session_ids = sorted(_gating_sessions(lines))
    return {"started": started, "ended": ended, "exit": code, "claude_session": init.get("session_id"),
            "session_id": session_ids[-1] if session_ids else None, "all_sessions": session_ids,
            "total_cost_usd": result.get("total_cost_usd"), "duration_api_ms": result.get("duration_api_ms")}


def _gating_sessions(lines) -> set:
    return set(re.findall(r"gs_\d{8}T\d{6}_[0-9a-f]{6}", "\n".join(json.dumps(x) for x in lines)))


def run(args) -> int:
    s = spec()
    name = args.run
    out = campaign_dir(args) / name
    if out.exists() and not args.force:
        print(f"{out} exists (--force to replace)")
        return 1
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    before = clean(args) if s.get("clean") else _check_start(args, s)
    if args.cooldown:
        # Let the provider's 5-minute prompt cache of the previous run lapse, so
        # this run pays for its own prefix (`cache_carryover_tokens` otherwise).
        print(f"cooling down {args.cooldown:g} min")
        time.sleep(args.cooldown * 60)
    label = f"bench-{args.campaign}-{name}"
    meta = {"run": name, "arm": "harness" if name.startswith("H") else "claude", "label": label,
            "gate_revision_before": before, **code_state()}
    print(f"{name}: {meta['arm']} as {label}")
    info = (run_harness if meta["arm"] == "harness" else run_claude)(args, s, name, out, label)
    meta.update(info)
    meta["gate_revision_after"] = gate_revision(s)
    meta["gates_unchanged"] = meta["gate_revision_after"] == before
    _json(out / "meta.json", meta)
    print(json.dumps({k: meta[k] for k in ("exit", "session_id", "gates_unchanged")}))
    print(f"wall {meta['ended'] - meta['started']:.0f} s")
    return 0 if meta["exit"] == 0 and meta["gates_unchanged"] else 1


# -- collect -------------------------------------------------------------------------------


def _price(prices, model):
    model = model or ""
    match = max((p for p in prices if model.startswith(p)), key=len, default=None)
    return prices.get(match) if match else None


def carryover(calls, prices) -> dict:
    """What a run read from cache on its FIRST call to each model: a prefix
    some earlier run (or the other arm) wrote. That makes it cheaper than it
    would be alone; `adjusted_cost_usd` prices those tokens as the cache
    writes they would otherwise have been."""
    first: dict = {}
    for c in calls:
        model = c.get("served_model") or (c.get("request") or {}).get("model") or "?"
        first.setdefault(model, c)
    tokens = {m: int((c.get("usage") or {}).get("cache_read", 0)) for m, c in first.items()}
    extra = 0.0
    for model, n in tokens.items():
        p = _price(prices, model)
        if p and n:
            extra += n * (p["cache_write_5m"] - p["cache_read"]) / 1e6
    return {"tokens": sum(tokens.values()), "by_model": tokens, "extra_usd": round(extra, 4)}


def call_cost(prices, model, u) -> float | None:
    p = _price(prices, model)
    if not p or not u:
        return None
    return (u.get("input_uncached", 0) * p["input"] + u.get("output", 0) * p["output"]
            + u.get("cache_read", 0) * p["cache_read"] + u.get("cache_write_5m", 0) * p["cache_write_5m"]
            + u.get("cache_write_1h", 0) * p["cache_write_1h"]) / 1e6


def _union(intervals) -> float:
    total, end = 0.0, None
    for a, b in sorted(intervals):
        if end is None or a > end:
            total += b - a
            end = b
        elif b > end:
            total += b - end
            end = b
    return total


def _peak(intervals) -> int:
    events = sorted([(a, 1) for a, b in intervals] + [(b, -1) for a, b in intervals])
    peak = now = 0
    for _, d in events:
        now += d
        peak = max(peak, now)
    return peak


def proxy_calls(out: Path) -> list:
    path = out / "proxy" / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def is_model_call(row) -> bool:
    return row.get("method") == "POST" and "messages" in row.get("path", "") and "count_tokens" not in row["path"]


def tokens_block(calls, prices) -> dict:
    keys = ("input_uncached", "cache_read", "cache_write_5m", "cache_write_1h", "output")
    total = {k: sum((c.get("usage") or {}).get(k, 0) for c in calls) for k in keys}
    written = total["cache_write_5m"] + total["cache_write_1h"]
    denominator = total["input_uncached"] + total["cache_read"] + written
    costs = [call_cost(prices, c.get("served_model") or (c.get("request") or {}).get("model"), c.get("usage"))
             for c in calls]
    return {**total, "cache_write": written, "input_total": denominator,
            "hit_rate": round(total["cache_read"] / denominator, 4) if denominator else None,
            "uncached_total": total["input_uncached"] + written,
            "cost_usd": round(sum(x for x in costs if x is not None), 4),
            "unpriced_calls": sum(1 for x in costs if x is None)}


def _session_record(session_id) -> dict | None:
    if not session_id:
        return None
    path = data_root() / ".agent/sessions/gating" / session_id / "session.json"
    return json.loads(path.read_text()) if path.exists() else None


def _decisions(session_id) -> list:
    path = data_root() / ".agent/sessions/gating" / (session_id or "-") / "decisions.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def kind_task() -> dict:
    from plexora.ai import tasks

    out = {}
    for task in tasks.tasks().values():
        if task.module == "gating":
            for kind in task.kinds:
                out[kind.split(":")[0]] = task.name
    return out


def collect_harness(out: Path, meta: dict, s: dict, calls: list) -> dict:
    """Per call: the trace's task and model beside the proxy's numbers."""
    from plexora.ai.harness.trace import TraceStore

    store = TraceStore()
    rows = []
    run_id = meta.get("harness_run_id")
    if run_id:
        with sqlite3.connect(store.path) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute("select * from model_calls where run_id=? order by seq", (run_id,))]
    _json(out / "trace_calls.json", rows)
    checks = []
    ktask = kind_task()
    for r in rows:
        if not r.get("model"):               # refused before a model answered: nothing to check
            continue
        task = (r.get("task") or "").split(".")[-1] or ktask.get(r.get("kind") or "")
        r["task"] = task
        want = s["models"].get(task or "")
        got = r.get("model") or ""
        checks.append({"seq": r.get("seq"), "packet": r.get("packet_id"), "kind": r.get("kind"),
                       "task": r.get("task"), "model": got, "want": want,
                       "ok": _same_model(got, want)})
    return {"trace_calls": len(rows), "model_checks": checks,
            "invalid": sum(1 for r in rows if not r.get("valid")),
            "charged_usd": sum(int(r.get("charged_micro") or 0) for r in rows) / 1e6}


def _same_model(got, want) -> bool:
    """`claude-haiku-4-5-20251001` is `claude-haiku-4-5`; `claude-sonnet-5-5` is
    not `claude-sonnet-5`."""
    got, want = got or "", want or ""
    return bool(want) and (got == want or got.startswith(want + "-20"))


def _transcripts(meta) -> list:
    sid = meta.get("claude_session")
    if not sid:
        return []
    found = list(Path.home().glob(f".claude/projects/*/{sid}.jsonl"))
    if not found:
        return []
    main = found[0]
    return [main] + sorted((main.parent / sid / "subagents").glob("*.jsonl"))


KIND_RE = re.compile(r'kind\\*"\s*:\s*\\*"(\w+)')


def collect_claude(out: Path, meta: dict, s: dict, calls: list) -> dict:
    """Workers: each subagent transcript's model and the packet kinds it fetched
    (a worker's gating_next results); tool timings from tool_use -> tool_result."""
    files = _transcripts(meta)
    for f in files:
        dst = out / "transcripts" / f.name
        dst.parent.mkdir(exist_ok=True)
        shutil.copy2(f, dst)
    ktask = kind_task()
    workers, tool_calls, tool_seconds = [], {}, 0.0
    checks = []
    for f in files:
        lines = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
        models = sorted({(x.get("message") or {}).get("model") for x in lines
                         if x.get("type") == "assistant" and (x.get("message") or {}).get("model")})
        uses = {}
        kinds = []
        for x in lines:
            message = x.get("message") or {}
            ts = x.get("timestamp")
            for block in message.get("content") or () if isinstance(message.get("content"), list) else ():
                if block.get("type") == "tool_use":
                    uses[block["id"]] = (block.get("name"), ts)
                    name = (block.get("name") or "").split("__")[-1]
                    tool_calls[name] = tool_calls.get(name, 0) + 1
                elif block.get("type") == "tool_result" and block.get("tool_use_id") in uses:
                    name, t0 = uses[block["tool_use_id"]]
                    try:
                        t0d = dt.datetime.fromisoformat(t0.replace("Z", "+00:00"))
                        t1d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if not name == "Agent":
                            tool_seconds += (t1d - t0d).total_seconds()
                    except Exception:      # noqa: BLE001
                        pass
                    if (name or "").endswith(("gating_next", "gating_session_start", "gating_answer")):
                        text = json.dumps(block.get("content"))
                        for match in KIND_RE.finditer(text):
                            if match.group(1) in ktask:
                                kinds.append(match.group(1))
                                break
        role = "coordinator" if f == files[0] else "worker"
        workers.append({"file": f.name, "role": role, "models": models, "packet_kinds": kinds})
        for kind in kinds:
            task = ktask.get(kind)
            want = s["models"].get(task)
            got = models[0] if models else ""
            checks.append({"file": f.name, "role": role, "kind": kind, "task": task, "model": got, "want": want,
                           "ok": _same_model(got, want)})
    agent_launches = []
    stream = [json.loads(x) for x in (out / "stream.jsonl").read_text().splitlines() if x.strip().startswith("{")]
    for x in stream:
        message = x.get("message") or {}
        for block in message.get("content") or () if isinstance(message.get("content"), list) else ():
            if block.get("type") == "tool_use" and block.get("name") in ("Agent", "Task"):
                inp = block.get("input") or {}
                agent_launches.append({"model": inp.get("model"), "subagent_type": inp.get("subagent_type"),
                                       "tasks_line": next((l for l in (inp.get("prompt") or "").splitlines()
                                                           if l.startswith("tasks:")), None),
                                       "background": inp.get("run_in_background")})
    cost_tool = None
    if files:
        try:
            r = subprocess.run([sys.executable, str(ROOT / "tools/transcript_cost.py"), str(files[0]), "--json"],
                               capture_output=True, text=True)
            cost_tool = json.loads(r.stdout)
        except Exception:                  # noqa: BLE001
            cost_tool = None
    return {"workers": workers, "agent_launches": agent_launches, "tool_calls": tool_calls,
            "tool_seconds_serial": round(tool_seconds, 1), "model_checks": checks,
            "transcript_cost": cost_tool, "claude_reported_cost_usd": meta.get("total_cost_usd")}


def collect(args) -> int:
    s = spec()
    base = campaign_dir(args)
    for out in sorted(p for p in base.iterdir() if (p / "meta.json").exists()):
        meta = json.loads((out / "meta.json").read_text())
        calls = [c for c in proxy_calls(out) if is_model_call(c)]
        wall = meta["ended"] - meta["started"]
        spans = [(c["t_start"], c["t_end"]) for c in calls if c.get("t_end")]
        by_model: dict = {}
        for c in calls:
            m = c.get("served_model") or (c.get("request") or {}).get("model") or "?"
            by_model.setdefault(m, []).append(c)
        aux = [c for c in calls if meta["arm"] == "claude" and not (c.get("request") or {}).get("n_tools")]
        core = [c for c in calls if c not in aux]
        record = _session_record(meta.get("session_id"))
        metrics = {
            "run": meta["run"], "arm": meta["arm"], "session_id": meta.get("session_id"),
            "wall_s": round(wall, 1),
            "model_calls": len(calls), "aux_calls": len(aux),
            "inference_busy_s": round(_union(spans), 1),
            "inference_sum_s": round(sum(b - a for a, b in spans), 1),
            "non_inference_s": round(wall - _union(spans), 1),
            "peak_concurrent_calls": _peak(spans),
            "ttft_median_s": _median([c["t_first_token"] - c["t_start"] for c in calls if c.get("t_first_token")]),
            "tokens": tokens_block(calls, s["prices"]),
            "tokens_core": tokens_block(core, s["prices"]),
            "cache_carryover": carryover(calls, s["prices"]),
            "by_model": {m: {"calls": len(v), **tokens_block(v, s["prices"])} for m, v in by_model.items()},
            "images": sum(c["request"].get("n_images", 0) for c in calls),
            "image_bytes": sum(c["request"].get("image_bytes", 0) for c in calls),
            "image_est_tokens": sum(c["request"].get("image_est_tokens", 0) for c in calls),
            "unique_images": len({i["image_sha"] for c in calls for i in c["request"].get("images", ())}),
            "retries": sum(1 for c in calls if c["request"].get("repeat_key") or c["request"].get("repeat_body")
                           or (c["request"].get("retry_header") not in (None, "0"))),
            "http_errors": sum(1 for c in calls if (c.get("status") or 0) >= 400),
            "effort": sorted({str(c["request"].get("effort")) for c in calls}),
            "thinking": sorted({json.dumps(c["request"].get("thinking")) for c in calls}),
            "system_chars": sorted({c["request"].get("system_chars") for c in calls}),
            "tools_chars": sorted({c["request"].get("tools_chars") for c in calls}),
            "timeline": [{"n": c["n"], "t": round(c["t_start"] - meta["started"], 2),
                          "dur": round(c["t_end"] - c["t_start"], 2) if c.get("t_end") else None,
                          "model": c.get("served_model"), "task": c["request"].get("task"),
                          "msgs": c["request"].get("n_messages"), "imgs": c["request"].get("n_images"),
                          "usage": c.get("usage"), "status": c.get("status")} for c in calls],
            "session": _session_summary(record, meta.get("session_id")),
        }
        arm = (collect_harness if meta["arm"] == "harness" else collect_claude)(out, meta, s, calls)
        metrics["arm_detail"] = arm
        metrics["tokens"]["adjusted_cost_usd"] = round(metrics["tokens"]["cost_usd"]
                                                       + metrics["cache_carryover"]["extra_usd"], 4)
        checks = arm.get("model_checks") or []
        metrics["model_check"] = {"checked": len(checks), "mismatches": [c for c in checks if not c["ok"]]}
        _json(out / "metrics.json", metrics)
        print(f"{meta['run']}: {len(calls)} calls, ${metrics['tokens']['cost_usd']}, wall {wall:.0f}s, "
              f"hit {metrics['tokens']['hit_rate']}, model mismatches {len(metrics['model_check']['mismatches'])}")
    return 0


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return round(values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2, 2)


def _session_summary(record, session_id) -> dict:
    if not record:
        return {}
    units = {}
    for u in record["units"].values():
        units[u["marker"]] = {k: u.get(k) for k in ("state", "final", "candidate", "confidence", "tier",
                                                   "method", "reason", "rounds", "class", "biology_fit")}
        units[u["marker"]]["packets"] = len(u.get("packets") or ())
    kinds: dict = {}
    for d in _decisions(session_id):
        if d.get("event") == "issued":
            kinds[d.get("kind")] = kinds.get(d.get("kind"), 0) + 1
    return {"state": record.get("state"), "units": units, "packet_kinds": kinds,
            "used": record.get("used"), "invalid_answers": record.get("invalid_answers"),
            "biology": record.get("biology"), "options": {k: (record.get("options") or {}).get(k) for k in
                                                          ("mode", "qc", "reuse_answers", "seed", "on_limit",
                                                           "agent", "reading", "evidence", "sheets")}}


# -- quality and report ------------------------------------------------------------------


def quality(s, runs: dict) -> dict:
    """Per-cell comparisons on every cell of the image: each run vs the
    reference, every pair of runs, and the co-expression checks."""
    import numpy as np

    from plexora.agent import AgentSession, registry
    from plexora.ai import bench

    registry.discover(["gating"])
    session = AgentSession(table_limit=4)
    ds = session.data(s["project"])
    ref = json.loads((ROOT / s["reference"]).read_text())["markers"]
    values = {m: np.asarray(ds.table.columns([m])[m], dtype=np.float32) for m in s["markers"]}
    gates = {"REF": {m: ref[m]["low"] for m in s["markers"] if m in ref}}
    for name, metrics in runs.items():
        gates[name] = {m: (u.get("final") if u.get("state") == "accepted" else u.get("final"))
                       for m, u in (metrics["session"].get("units") or {}).items()}

    def positive(name, m):
        low = gates[name].get(m)
        if low is None:
            return None
        return values[m] > np.float32(low)

    out: dict = {"gates": gates, "fraction": {}, "pairs": {}, "coexpression": {}}
    for name in gates:
        out["fraction"][name] = {m: (float(positive(name, m).mean()) if positive(name, m) is not None else None)
                                 for m in s["markers"]}
    names = list(gates)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            per = {}
            for m in s["markers"]:
                pa, pb = positive(a, m), positive(b, m)
                if pa is None or pb is None:
                    per[m] = None
                    continue
                c = bench.classification(values[m], pb, gates[a][m])
                inter = int((pa & pb).sum())
                union = int((pa | pb).sum())
                per[m] = {"f1": round(c["f1"], 4), "kappa": round(c["kappa"], 4),
                          "jaccard": round(inter / union, 4) if union else 1.0,
                          "delta_low": round(float(gates[a][m]) - float(gates[b][m]), 3)}
            out["pairs"][f"{a}~{b}"] = per
    rules = [("CD8a", "CD3e", "CD8a+ that are CD3e+"), ("CD3e", "CD45", "CD3e+ that are CD45+"),
             ("FOXP3", "CD3e", "FOXP3+ that are CD3e+")]
    for name in gates:
        row = {}
        for child, parent, label in rules:
            pc, pp = positive(name, child), positive(name, parent)
            row[label] = round(float((pc & pp).sum() / pc.sum()), 4) if pc is not None and pp is not None \
                and pc.sum() else None
        ps, pl = positive(name, "SOX10"), positive(name, "CD45")
        row["SOX10+CD45+ / SOX10+"] = round(float((ps & pl).sum() / ps.sum()), 4) \
            if ps is not None and pl is not None and ps.sum() else None
        out["coexpression"][name] = row
    return out


def _arm_spread(pairs, markers, kind):
    """Median F1 over markers for within-arm vs between-arm pairs."""
    import statistics

    groups = {"within_H": [], "within_C": [], "between": [], "vs_REF_H": [], "vs_REF_C": []}
    for key, per in pairs.items():
        a, b = key.split("~")
        if "REF" in (a, b):
            other = b if a == "REF" else a
            target = "vs_REF_H" if other.startswith("H") else "vs_REF_C"
        elif a[0] == b[0]:
            target = f"within_{a[0]}"
        else:
            target = "between"
        for m in markers:
            if per.get(m):
                groups[target].append(per[m][kind])
    return {k: (round(statistics.median(v), 4) if v else None) for k, v in groups.items()}


def report(args) -> int:
    s = spec()
    base = campaign_dir(args)
    runs = {}
    for name in s["order"]:
        path = base / name / "metrics.json"
        if path.exists():
            runs[name] = json.loads(path.read_text())
    q = quality(s, runs)
    q["spread_f1"] = _arm_spread(q["pairs"], s["markers"], "f1")
    q["spread_jaccard"] = _arm_spread(q["pairs"], s["markers"], "jaccard")
    _json(base / "quality.json", q)
    _json(base / "comparison.json", {"spec": s, "runs": {k: {kk: vv for kk, vv in v.items() if kk != "timeline"}
                                                          for k, v in runs.items()}, "quality": q})
    md = base / f"tables_{args.campaign}.md"
    md.write_text(to_markdown(s, runs, q))
    print(f"tables -> {md}")
    print(json.dumps({"spread_f1": q["spread_f1"], "spread_jaccard": q["spread_jaccard"]}, indent=1))
    return 0


def _f(value, digits=2):
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _table(header, rows) -> list:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + \
        ["| " + " | ".join(_f(c) for c in row) + " |" for row in rows]


def to_markdown(s, runs, q) -> str:
    """The data tables of the report; the narrative is written around them."""
    names = list(runs)
    out = ["## Efficiency per run", ""]
    rows = []
    for n, m in runs.items():
        t = m["tokens"]
        rows.append([n, m["arm"], m["wall_s"], m["inference_busy_s"], m["non_inference_s"], m["model_calls"],
                     m["aux_calls"], t["input_uncached"], t["cache_read"], t["cache_write"], t["output"],
                     f"{t['hit_rate']:.1%}" if t["hit_rate"] is not None else "-", t["uncached_total"],
                     f"${t['cost_usd']:.2f}", m["peak_concurrent_calls"], m["images"], m["image_est_tokens"],
                     m["retries"], m["http_errors"]])
    out += _table(["run", "arm", "wall s", "inference busy s", "other s", "model calls", "client aux calls",
                   "input uncached", "cache read", "cache write", "output", "hit rate", "uncached total",
                   "list cost", "peak concurrent", "images", "image tokens (est)", "retries", "HTTP errors"], rows)
    out += ["", "## Cache carried over from earlier runs", "",
            "Tokens each run read from cache on its first call to a model: a prefix an earlier run wrote. "
            "The adjusted cost prices them as the writes they would have been in a run alone.", ""]
    rows = [[n, m.get("cache_carryover", {}).get("tokens", 0),
             ", ".join(f"{k} {v:,}" for k, v in (m.get("cache_carryover", {}).get("by_model") or {}).items() if v),
             f"${m['tokens']['cost_usd']:.2f}",
             f"${m['tokens'].get('adjusted_cost_usd', m['tokens']['cost_usd']):.2f}"] for n, m in runs.items()]
    out += _table(["run", "carried-over tokens", "by model", "list cost", "adjusted cost"], rows)
    out += ["", "## Tokens and cost by model", ""]
    rows = []
    for n, m in runs.items():
        for model, b in sorted(m["by_model"].items()):
            rows.append([n, model, b["calls"], b["input_uncached"], b["cache_read"], b["cache_write"], b["output"],
                         f"{b['hit_rate']:.1%}" if b["hit_rate"] is not None else "-", f"${b['cost_usd']:.2f}"])
    out += _table(["run", "model", "calls", "input uncached", "cache read", "cache write", "output", "hit rate",
                   "list cost"], rows)
    out += ["", "## Model routing check (every packet's task -> model)", ""]
    rows = [[n, m["model_check"]["checked"], len(m["model_check"]["mismatches"]),
             "; ".join(sorted({f"{c['task']}->{c['model']}" for c in m["model_check"]["mismatches"]}))[:200]]
            for n, m in runs.items()]
    out += _table(["run", "calls checked", "mismatches", "which"], rows)
    out += ["", "## What each arm sent (per call, distinct values)", ""]
    rows = [[n, ", ".join(map(str, m["system_chars"]))[:60], ", ".join(map(str, m["tools_chars"]))[:60],
             ", ".join(m["effort"]), ", ".join(m["thinking"])[:80]] for n, m in runs.items()]
    out += _table(["run", "system chars", "tool schema chars", "effort", "thinking"], rows)
    out += ["", "## Session outcome per marker", ""]
    rows = []
    for mk in s["markers"]:
        ref = q["gates"]["REF"].get(mk)
        row = [mk, ref]
        for n in names:
            u = (runs[n]["session"].get("units") or {}).get(mk) or {}
            row.append(f"{_f(u.get('final'))} {u.get('state') or '-'} {u.get('tier') or ''} "
                       f"{u.get('confidence') or ''} ({u.get('packets', 0)} pk)")
        rows.append(row)
    out += _table(["marker", "reference low"] + names, rows)
    out += ["", "## Positive fraction (all cells)", ""]
    rows = [[mk] + [f"{q['fraction'][n][mk]:.2%}" if q["fraction"][n].get(mk) is not None else "-"
                    for n in ["REF"] + names] for mk in s["markers"]]
    out += _table(["marker", "REF"] + names, rows)
    out += ["", "## Per-cell agreement, every pair (F1 / Jaccard / delta low)", ""]
    rows = []
    for key, per in q["pairs"].items():
        rows.append([key] + [f"{v['f1']:.3f} / {v['jaccard']:.3f} / {v['delta_low']:+.2f}" if v else "-"
                             for v in (per.get(mk) for mk in s["markers"])])
    out += _table(["pair"] + s["markers"], rows)
    out += ["", "## Spread: within arm vs between arms (median over markers and pairs)", ""]
    rows = [[k, q["spread_f1"].get(k), q["spread_jaccard"].get(k)] for k in q["spread_f1"]]
    out += _table(["group", "F1", "Jaccard"], [[r[0], r[1], r[2]] for r in rows])
    out += ["", "## Co-expression and exclusion checks", ""]
    keys = list(next(iter(q["coexpression"].values())).keys())
    rows = [[n] + [f"{v:.1%}" if v is not None else "-" for v in (q["coexpression"][n][k] for k in keys)]
            for n in q["coexpression"]]
    out += _table(["run"] + keys, rows)
    out += ["", "## Packets by kind", ""]
    kinds = sorted({k for m in runs.values() for k in (m["session"].get("packet_kinds") or {})})
    rows = [[n] + [(m["session"].get("packet_kinds") or {}).get(k, 0) for k in kinds] for n, m in runs.items()]
    out += _table(["run"] + kinds, rows)
    out += ["", "## Claude Code tool calls", ""]
    for n, m in runs.items():
        if m["arm"] == "claude":
            d = m["arm_detail"]
            out.append(f"- {n}: " + ", ".join(f"{k} {v}" for k, v in sorted(d["tool_calls"].items())) +
                       f"; workers launched {len(d['agent_launches'])} (models "
                       f"{', '.join(str(a['model']) for a in d['agent_launches'])}); serial tool time "
                       f"{d['tool_seconds_serial']} s; Claude Code's own cost figure "
                       f"${_f(d['claude_reported_cost_usd'])}")
        else:
            d = m["arm_detail"]
            out.append(f"- {n}: harness trace {d['trace_calls']} calls, {d['invalid']} invalid, "
                       f"gateway charged ${d['charged_usd']:.2f}")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--campaign", default=dt.date.today().isoformat())
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    p = sub.add_parser("snapshot")
    p.add_argument("--force", action="store_true")
    sub.add_parser("restore")
    sub.add_parser("clean")
    p = sub.add_parser("run")
    p.add_argument("run")
    p.add_argument("--force", action="store_true")
    p.add_argument("--cooldown", type=float, default=0, metavar="MIN",
                   help="Wait this many minutes after cleaning, before the run (lets an earlier "
                        "run's 5-minute prompt cache lapse).")
    sub.add_parser("collect")
    sub.add_parser("report")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    return {"preflight": preflight, "snapshot": snapshot, "restore": restore, "run": run, "collect": collect,
            "report": report, "clean": lambda a: (clean(a), 0)[1]}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())

"""End-to-end pipeline check for Plexora AI, on free models or a local stub.

    python tools/ai_e2e.py --stub                 # no network, no key: a local fake OpenRouter
    python tools/ai_e2e.py --live                 # OpenRouter's free models (key in licensing/.dev.vars
                                                  # as OPENROUTER_API_KEY=..., or in the environment)
    python tools/ai_e2e.py --live --remote staging  # the same checks against the deployed STAGING Worker
                                                  # (tools/ai_staging.py; its own key, D1 and secrets)

What runs is the real thing, locally: the licence Worker under `wrangler dev
--local` with its own throwaway D1 (nothing deployed, the production database
never touched), a real certificate activated against it, a real PLXAI1
token, the real route table, and Plexora's own harness and chat agent. Only
the model is cheap: OpenRouter's `:free` models (`--live`) or a stub that
speaks OpenRouter's wire (`--stub`).

With `--remote` the Worker is the deployed staging copy instead: the checks are
the same, the admin calls use staging's ADMIN_TOKEN (~/.plexora-staging/), the
accounting SQL goes through `wrangler d1 execute --env staging --remote` on the
staging database by name, and this process trusts staging's own signing key
(pxs1) in memory only. Each run issues its own licence, so the accounting is
exact; the routes it publishes replace staging's at the same slots. The
production host is refused.

Accuracy is NOT measured. The checks are about the pipeline:

  stream        a streamed call: Anthropic-shaped events, a usage event, a request row
  structured    a call with an answer schema reaches the model; whether the reply parsed is reported
  tool_use      a chat turn calls a tool and receives its result
  gating        a gating session on a synthetic image reaches a terminal state, every call recorded
  qc            the same for an AutoQC session (the QC worker)
  failover      with rank 0 switched off, rank 1 serves and the row says failover
  retries       a 429 is retried on the same route (forced in --stub; observed if it happens in --live)
  shadow        a sampled call is duplicated to a shadow route, recorded, never billed
  accounting    ledger = balance, settlements = charges, no hold left open, tokens agree with the trace

Free models cost $0, so they are catalogued at NOMINAL prices (--price) to
make the ledger move; the price is labelled as a test price in the catalogue.
Routes to them are published unbenched, which only this local Worker allows
(AI_ALLOW_UNBENCHED_ROUTES=1 on the command line, never in wrangler.toml).

The report goes to --out (default <tmp>/plexora-ai-e2e/report.{json,md}).
Exit status 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LICENSING = ROOT / "licensing"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: The test signing seed (licensing/test/routes/keys.ts): a local Worker only.
SIGNING = {"ACTIVE_KID": "pxt", "PUBLIC_KEYS_JSON": "",
           "SIGNING_KEY_PXT": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"}
ADMIN = "e2e-admin"
MARKERS = ("CD3", "CD8")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body=None, headers=None, timeout=60):
    data = None if body is None else json.dumps(body).encode("utf-8")
    # A named agent: Cloudflare's browser integrity check refuses `Python-urllib/*` (error 1010).
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", "User-Agent": "plexora-ai-e2e/1",
                                              **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw[:500].decode("utf-8", "replace")}


# -- the stub provider ------------------------------------------------------------------


class StubOpenRouter:
    """A local OpenRouter: Chat Completions SSE, a prompt-cache emulator, a
    tool call when tools are offered, packet answers from a brain, and 429s
    on request (`fail_next`)."""

    def __init__(self):
        self.brain = None
        self.calls: list[dict] = []
        self.fail_next = 0
        self.seen: set[str] = set()
        self.lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                with stub.lock:
                    stub.calls.append({"path": self.path, "body": body,
                                       "auth": self.headers.get("Authorization")})
                    fail = stub.fail_next > 0
                    if fail:
                        stub.fail_next -= 1
                if fail:
                    data = b'{"error":{"message":"rate limited","code":429}}'
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in stub.reply(body):
                    self.wfile.write(f"data: {chunk if isinstance(chunk, str) else json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _usage(self, body) -> dict:
        """Prompt-cache emulation: the longest previously seen message prefix (with the tools) is read."""
        tools = json.dumps(body.get("tools") or [], sort_keys=True)
        total = max(1, int(len(tools + json.dumps(body["messages"])) / 3.5))
        cached = 0
        with self.lock:
            for i in range(1, len(body["messages"]) + 1):
                key = hashlib.sha256((tools + json.dumps(body["messages"][:i], sort_keys=True)).encode()).hexdigest()
                if key in self.seen and i < len(body["messages"]):
                    cached = int(len(tools + json.dumps(body["messages"][:i])) / 3.5)
                self.seen.add(key)
        cached = min(cached, total - 1)
        return {"prompt_tokens": total, "completion_tokens": 40, "total_tokens": total + 40,
                "prompt_tokens_details": {"cached_tokens": cached}, "cost": 0}

    def _packet(self, body):
        for message in reversed(body["messages"]):
            if message.get("role") != "user":
                continue
            parts = message["content"] if isinstance(message["content"], list) else [
                {"type": "text", "text": message["content"]}]
            for part in parts:
                if part.get("type") == "text":
                    try:
                        value = json.loads(part["text"])
                    except ValueError:
                        continue
                    if isinstance(value, dict) and "packet_id" in value:
                        return value
            return None
        return None

    def reply(self, body):
        head = {"id": f"gen-{len(self.calls)}", "model": body.get("model")}
        last = body["messages"][-1]
        if body.get("tools") and last.get("role") == "user":
            name = next((t["function"]["name"] for t in body["tools"] if t["function"]["name"] == "list_skills"),
                        body["tools"][0]["function"]["name"])
            yield {**head, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Checking. "}}]}
            yield {**head, "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_e2e",
                    "type": "function", "function": {"name": name, "arguments": "{}"}}]}}]}
            yield {**head, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}
        else:
            packet = self._packet(body)
            if packet is not None and self.brain:
                text = json.dumps(self.brain(packet))
            elif body.get("response_format") or "JSON" in json.dumps(body["messages"][-1])[-400:]:
                text = '```json\n{"answer": "ok"}\n```'
            else:
                text = "The pipeline works."
            for i in range(0, len(text), 24):
                yield {**head, "choices": [{"index": 0, "delta": {"content": text[i:i + 24]}}]}
            yield {**head, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield {**head, "choices": [], "usage": self._usage(body)}
        yield "[DONE]"


# -- the local licence Worker -------------------------------------------------------------


class LocalWorker:
    def __init__(self, workdir: Path, extra_vars: dict, log: Path):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.state = workdir / "d1"
        self.log = log
        self.vars = {**SIGNING, "ADMIN_TOKEN": ADMIN, "FP_PEPPER": "e2e-fp", "IP_HASH_KEY": "e2e-ip",
                     "SESSION_KEY": "e2e-session", "PUBLIC_BASE_URL": self.url, "AI_USER_PEPPER": "e2e-pepper",
                     "AI_ALLOW_UNBENCHED_ROUTES": "1", "AI_RETRY_BACKOFF_MS": "200", **extra_vars}
        self.process = None

    def _npx(self, *args, **kw):
        npx = shutil.which("npx") or shutil.which("npx.cmd")
        return subprocess.run([npx, *args], cwd=LICENSING, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", **kw)

    def start(self):
        if not (LICENSING / "node_modules" / ".bin").exists():
            raise SystemExit("licensing/node_modules has no .bin: run `npm ci` in licensing/ first.")
        done = self._npx("wrangler", "d1", "execute", "plexora-license", "--local", "--persist-to", str(self.state),
                         "--file=schema.sql", timeout=180)
        if done.returncode:
            raise SystemExit(f"could not create the local D1:\n{done.stdout}\n{done.stderr}")
        npx = shutil.which("npx") or shutil.which("npx.cmd")
        args = [npx, "wrangler", "dev", "--local", "--ip", "127.0.0.1", "--port", str(self.port),
                "--inspector-port", str(free_port()), "--persist-to", str(self.state),
                "--show-interactive-dev-session=false"]
        for key, value in self.vars.items():
            args += ["--var", f"{key}:{value}"]
        self.process = subprocess.Popen(args, cwd=LICENSING, stdout=self.log.open("w", encoding="utf-8"),
                                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        deadline = time.time() + 120
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise SystemExit(f"wrangler dev exited; see {self.log}")
            with contextlib.suppress(Exception):
                if http("GET", f"{self.url}/healthz", timeout=3)[0] == 200:
                    return
            time.sleep(1)
        raise SystemExit(f"wrangler dev did not come up in 120 s; see {self.log}")

    def sql(self, query: str) -> list[dict]:
        done = self._npx("wrangler", "d1", "execute", "plexora-license", "--local", "--persist-to", str(self.state),
                         "--json", "--command", query, timeout=120)
        if done.returncode:
            raise RuntimeError(done.stderr or done.stdout)
        out = json.loads(done.stdout)
        return out[0]["results"] if isinstance(out, list) else out["results"]

    def stop(self):
        if self.process and self.process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.process.pid)], capture_output=True)
            else:
                self.process.terminate()
            with contextlib.suppress(Exception):
                self.process.wait(10)

    def admin(self, method, path, body=None):
        return http(method, f"{self.url}/admin/api{path}", body, {"Authorization": f"Bearer {ADMIN}"})

    def trusted_keys(self) -> dict[str, str]:
        """The public half of the local test seed."""
        import base64

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        seed = SIGNING["SIGNING_KEY_PXT"]
        private = Ed25519PrivateKey.from_private_bytes(base64.urlsafe_b64decode(seed + "=" * (-len(seed) % 4)))
        public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return {"pxt": base64.urlsafe_b64encode(public).decode().rstrip("=")}


class RemoteWorker:
    """A deployed STAGING Worker (tools/ai_staging.py): the admin API with staging's
    token, SQL through `wrangler d1 execute --env staging --remote` on the staging
    database by name, and trust in staging's own signing key. Nothing to start or
    stop; refuses the production host."""

    def __init__(self, url: str):
        from tools import ai_staging

        self.staging = ai_staging
        self.cfg = ai_staging.config()
        ai_staging.require_safe(self.cfg)
        self.url = ai_staging.staging_url(url)
        self.token = ai_staging.admin_token()

    def start(self):
        status, _ = http("GET", f"{self.url}/healthz", timeout=15)
        if status != 200:
            raise SystemExit(f"{self.url}/healthz answered {status}")

    def stop(self):
        pass

    def sql(self, query: str) -> list[dict]:
        return self.staging.sql(self.cfg, query)

    def admin(self, method, path, body=None):
        return http(method, f"{self.url}/admin/api{path}", body, {"Authorization": f"Bearer {self.token}"})

    def trusted_keys(self) -> dict[str, str]:
        return json.loads(self.cfg["staging"]["vars"]["PUBLIC_KEYS_JSON"])


# -- seeding (shared with tools/ai_staging.py) ---------------------------------------------


def catalogue_models(admin, models, price: int, label: str = "e2e only") -> None:
    """Catalogue free OpenRouter models at a NOMINAL test price, labelled as such."""
    for model in {m["id"]: m for m in models}.values():
        status, body = admin("PUT", f"/ai/models/openrouter/{model['id']}", {
            "in_micro": price, "cache_read_micro": price // 10, "cache_write_5m_micro": price * 5 // 4,
            "cache_write_1h_micro": price * 2, "out_micro": price * 5, "fee_bps": 0,
            "supports_structured": model["structured"], "supports_tools": model["tools"],
            "supports_vision": model["vision"],
            "source_url": f"https://openrouter.ai/models (free tier: NOMINAL test price, {label})"})
        assert status == 200, body


def publish_routes(admin, text, vision, fallback) -> list[dict]:
    """Rank-0 routes for the text and vision capabilities, with the fallback at rank 1.
    Each replaces the row at its slot, so re-seeding is safe."""
    routes = []
    if text:
        for cap in ("text_routine", "text_reasoning"):
            routes.append({"capability": cap, "provider": "openrouter", "model": text["id"], "rank": 0})
    if vision:
        for cap in ("vision_judgement", "vision_routine"):
            routes.append({"capability": cap, "provider": "openrouter", "model": vision["id"], "rank": 0})
            if fallback:
                routes.append({"capability": cap, "provider": "openrouter", "model": fallback["id"], "rank": 1})
    published = []
    for route in routes:
        status, body = admin("POST", "/ai/routes", {**route, "max_tokens_cap": 4096})
        assert status == 201, body
        published.append(body)
    return published


# -- free-model discovery -----------------------------------------------------------------


def discover_free_models(key: str | None) -> list[dict]:
    """OpenRouter's $0 models with what they support, from its public model list."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    status, body = http("GET", "https://openrouter.ai/api/v1/models", headers=headers)
    if status != 200:
        raise SystemExit(f"could not list OpenRouter models: HTTP {status}")
    out = []
    for m in body.get("data") or []:
        pricing = m.get("pricing") or {}
        if not str(m.get("id", "")).endswith(":free"):
            continue
        if any(float(pricing.get(k) or 0) for k in ("prompt", "completion")):
            continue
        params = set(m.get("supported_parameters") or [])
        modalities = set((m.get("architecture") or {}).get("input_modalities") or [])
        out.append({"id": m["id"], "tools": "tools" in params,
                    "structured": bool({"response_format", "structured_outputs"} & params),
                    "vision": "image" in modalities, "context": m.get("context_length") or 0})
    return out


#: Classifiers and guard models answer "safe/unsafe", not the question: never a test model.
NOT_ASSISTANTS = ("safety", "guard", "moderation", "embed")


def pick(models, *, need, avoid=()) -> dict | None:
    """The free model that exercises most of the pipeline: native tools and
    structured output first, then context. A fallback from another vendor
    (`avoid` holds ids; their vendor prefix is avoided too when possible)."""
    vendors = {a.split("/")[0] for a in avoid if a}
    usable = [m for m in models if m["id"] not in avoid and all(m[k] for k in need)
              and not any(word in m["id"] for word in NOT_ASSISTANTS)]
    usable.sort(key=lambda m: (m["id"].split("/")[0] not in vendors, m["structured"], m["tools"], m["context"]),
                reverse=True)
    return usable[0] if usable else None


# -- the checks ---------------------------------------------------------------------------


class Checks:
    def __init__(self):
        self.results: list[dict] = []

    def run(self, name, fn):
        started = time.time()
        try:
            status, detail = fn()
        except Exception as exc:          # noqa: BLE001 -- a check that raises is a failed check, reported
            status, detail = "fail", {"error": f"{type(exc).__name__}: {exc}",
                                      "trace": traceback.format_exc()[-2000:]}
        self.results.append({"check": name, "status": status, "seconds": round(time.time() - started, 1),
                             "detail": detail})
        mark = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skip": "SKIP"}[status]
        print(f"  [{mark}] {name} ({self.results[-1]['seconds']} s)"
              + (f": {detail.get('error') or detail.get('note')}" if isinstance(detail, dict) and
                 (detail.get("error") or detail.get("note")) else ""), flush=True)
        return status, detail


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--stub", action="store_true", help="A local fake OpenRouter (no network, no key).")
    mode.add_argument("--live", action="store_true", help="OpenRouter's free models (needs a key).")
    parser.add_argument("--text-model", default=None, help="Override the free text model (OpenRouter id).")
    parser.add_argument("--vision-model", default=None, help="Override the free vision model.")
    parser.add_argument("--fallback-model", default=None, help="Override the free rank-1 model.")
    parser.add_argument("--price", type=int, default=1_000_000,
                        help="Nominal test price, micro-USD per 1M input tokens (output 5x; default $1).")
    parser.add_argument("--skip", default="", help="Comma-separated checks to skip.")
    parser.add_argument("--out", default=None)
    parser.add_argument("--keep", action="store_true", help="Leave the local Worker's D1 behind.")
    parser.add_argument("--remote", default=None, metavar="URL",
                        help="Run against a deployed STAGING Worker instead of a local one: its URL, or "
                             "`staging` for the one tools/ai_staging.py deployed. Needs --live (the stub is "
                             "local, so Cloudflare cannot reach it). Never production.")
    args = parser.parse_args(argv)
    if args.remote and args.stub:
        parser.error("--remote needs --live: a deployed Worker cannot reach the local stub")
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    work = Path(tempfile.mkdtemp(prefix="plexora-ai-e2e-"))
    out = Path(args.out) if args.out else work / "report"
    data_root = work / "data"
    data_root.mkdir()
    os.environ["PLEXORA_DATA_PATH"] = str(data_root)

    stub = StubOpenRouter() if args.stub else None
    extra = {}
    key = os.environ.get("OPENROUTER_API_KEY")
    if stub:
        extra = {"OPENROUTER_BASE_URL": stub.url, "OPENROUTER_API_KEY": "stub-key"}
    elif args.remote:
        pass                                  # the Worker's own secret; the model list is public
    elif key:
        extra = {"OPENROUTER_API_KEY": key}
    elif "OPENROUTER_API_KEY" not in ((LICENSING / ".dev.vars").read_text(encoding="utf-8")
                                       if (LICENSING / ".dev.vars").exists() else ""):
        raise SystemExit("--live needs OPENROUTER_API_KEY (environment, or licensing/.dev.vars).")

    if stub:
        models = [{"id": "stub/text:free", "tools": True, "structured": True, "vision": False, "context": 1},
                  {"id": "stub/vision:free", "tools": True, "structured": False, "vision": True, "context": 1},
                  {"id": "stub/fallback:free", "tools": True, "structured": True, "vision": True, "context": 1}]
    else:
        models = discover_free_models(key)
        print(f"{len(models)} free OpenRouter models found", flush=True)
    by_id = {m["id"]: m for m in models}
    text = by_id.get(args.text_model) if args.text_model else pick(models, need=("tools",))
    vision = by_id.get(args.vision_model) if args.vision_model else pick(models, need=("vision",))
    fallback = (by_id.get(args.fallback_model) if args.fallback_model
                else pick(models, need=("vision",), avoid={vision and vision["id"]}))
    if args.text_model and not text:
        text = {"id": args.text_model, "tools": True, "structured": False, "vision": False, "context": 0}
    if args.vision_model and not vision:
        vision = {"id": args.vision_model, "tools": False, "structured": False, "vision": True, "context": 0}
    print(f"text {text and text['id']}, vision {vision and vision['id']}, fallback {fallback and fallback['id']}",
          flush=True)

    worker = RemoteWorker(args.remote) if args.remote else LocalWorker(work, extra, work / "wrangler.log")
    checks = Checks()
    report = {"mode": "stub" if stub else "remote" if args.remote else "live",
              "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "worker": worker.url,
              "models": {"text": text, "vision": vision, "fallback": fallback}, "checks": checks.results}
    try:
        print(f"using the staging Worker at {worker.url}" if args.remote
              else f"starting the licence Worker locally ({work})", flush=True)
        worker.start()
        ctx = Context(worker, stub, text, vision, fallback, args.price, data_root)
        ctx.seed()
        for name, fn in (("stream", ctx.check_stream), ("structured", ctx.check_structured),
                         ("retries", ctx.check_retries), ("shadow", ctx.check_shadow),
                         ("tool_use", ctx.check_tool_use), ("gating", ctx.check_gating), ("qc", ctx.check_qc),
                         ("failover", ctx.check_failover), ("accounting", ctx.check_accounting)):
            if name in skip:
                checks.results.append({"check": name, "status": "skip", "seconds": 0, "detail": {}})
                continue
            checks.run(name, fn)
        report["calls"] = ctx.calls_table()
    finally:
        worker.stop()
        if stub:
            stub.close()
        report["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        out.parent.mkdir(parents=True, exist_ok=True)
        Path(f"{out}.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        Path(f"{out}.md").write_text(markdown(report), encoding="utf-8")
        print(f"report: {out}.md" + ("" if args.remote else f"  (wrangler log: {work / 'wrangler.log'})"), flush=True)
        if not args.keep:
            shutil.rmtree(work / "d1", ignore_errors=True)
    failed = [r for r in checks.results if r["status"] == "fail"]
    return 1 if failed else 0


class Context:
    def __init__(self, worker, stub, text, vision, fallback, price, data_root):
        self.w, self.stub, self.text, self.vision, self.fallback = worker, stub, text, vision, fallback
        self.price = price
        self.data_root = data_root
        self.request_ids: set[str] = set()

    # -- setup ------------------------------------------------------------------------

    def seed(self):
        status, issued = self.w.admin("POST", "/licenses", {"owner_email": "e2e@lab.example.org",
                                                            "use_class": "academic", "seats": 2, "days": 365,
                                                            "send_email": False})
        assert status == 201, issued
        self.account = issued["account_id"]
        self.activate(issued["seat"]["key"])
        assert self.w.admin("PATCH", f"/ai/accounts/{self.account}", {"mode": "dev",
                                                                       "notes": "e2e pipeline test"})[0] == 200
        assert self.w.admin("POST", f"/ai/accounts/{self.account}/credit",
                            {"credits": 10_000, "kind": "grant", "note": "e2e"})[0] == 201
        from plexora.ai.harness.gateway import TokenSource

        # The real token path: licence certificate -> client.ai_token -> PLXAI1.
        os.environ.pop("PLEXORA_AI_TOKEN", None)
        self.tokens = TokenSource()
        catalogue_models(self.w.admin, [m for m in (self.text, self.vision, self.fallback) if m], self.price)
        publish_routes(self.w.admin, self.text, self.vision, self.fallback)

    def activate(self, seat_key: str):
        """Activate THIS process's Plexora against the Worker, as `plexora license
        activate` would: a throwaway licence directory, that server, and trust in that
        Worker's key only (the local test key, or staging's pxs1; in this process,
        nothing is written to keys.py)."""
        os.environ["PLEXORA_LICENSE_DIR"] = str(self.data_root.parent / "license")
        os.environ["PLEXORA_LICENSE_SERVER"] = self.w.url
        os.environ.pop("PLEXORA_LICENSE_OFFLINE", None)
        os.environ.pop("PLEXORA_LICENSE_TOKEN", None)
        os.environ.pop("PLEXORA_LICENSE_FILE", None)
        from plexora import licensing
        from plexora.licensing import client, keys, state

        keys.PUBLIC_KEYS = self.w.trusted_keys()
        result = client.activate(seat_key, kind="desktop", name="e2e")
        state.save_activation(result, credential=seat_key, source="e2e")
        current = licensing.reload()
        assert current.certificate, f"activation did not take: {licensing.describe()}"
        assert licensing.allows("ai:gating:session"), f"no AI entitlement: {licensing.describe()}"

    def client(self):
        from plexora.ai.harness.gateway import GatewayClient

        return GatewayClient(self.w.url, tokens=self.tokens, dev=False, max_attempts=4, sleep=time.sleep)

    def rows(self, ids=None) -> list[dict]:
        status, body = self.w.admin("GET", f"/ai/requests?account_id={self.account}&limit=1000")
        assert status == 200, body
        rows = body["requests"]
        return [r for r in rows if ids is None or r["id"] in ids]

    def call(self, capability="text_routine", text="Say: the pipeline works.", schema=None, feature="e2e",
             session="e2e_1", key=None):
        from plexora.ai.harness.wire import ModelRequest, text_block

        request = ModelRequest(capability=capability, system=[text_block("You are a test endpoint for Plexora.")],
                               messages=[{"role": "user", "content": [text_block(text)]}], max_tokens=300,
                               output_schema=schema, context={"feature": feature, "agent": "e2e",
                                                              "session_id": session})
        response = self.client().messages(request, idempotency_key=key or f"e2e-{time.time_ns()}")
        self.request_ids.add(response.gateway_request_id)
        return response

    # -- checks -------------------------------------------------------------------------

    def check_stream(self):
        if not self.text:
            return "skip", {"note": "no free text model"}
        r = self.call()
        row = self.rows({r.gateway_request_id})
        assert row, "no ai_requests row"
        row = row[0]
        problems = []
        if not r.text.strip():
            problems.append("empty answer")
        if row["status"] != "ok":
            problems.append(f"row status {row['status']}")
        if (row["input_uncached"] + row["cache_read"]) <= 0 or row["output_tokens"] <= 0:
            problems.append("no token counts")
        if row["provider"] != "openrouter" or row["model"] != self.text["id"]:
            problems.append(f"served by {row['provider']}/{row['model']}")
        return ("fail" if problems else "pass"), {"error": "; ".join(problems) or None, "text": r.text[:200],
                                                  "usage": vars(r.usage) if hasattr(r.usage, "__dict__") else str(r.usage),
                                                  "charged_micro": r.charged_micro, "request": row["id"],
                                                  "attempts": row["attempts"]}

    def check_structured(self):
        if not self.text:
            return "skip", {"note": "no free text model"}
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
                  "additionalProperties": False}
        r = self.call(text="Is the pipeline working? Answer in the JSON object.", schema=schema)
        try:
            parsed = r.json()
            ok = isinstance(parsed, dict) and "answer" in parsed
        except ValueError:
            parsed, ok = None, False
        sent = None
        if self.stub:
            last = self.stub.calls[-1]["body"]
            sent = "response_format" if last.get("response_format") else "prompt"
        detail = {"parsed": parsed, "raw": r.text[:300], "schema_sent_as": sent or
                  ("response_format" if self.text["structured"] else "prompt")}
        # The pipeline delivered the schema and the answer; a free model's JSON is a quality note.
        return ("pass" if ok else "warn"), {**detail, "note": None if ok else "the model's reply did not parse"}

    def check_retries(self):
        if not self.text:
            return "skip", {"note": "no free text model"}
        if self.stub:
            self.stub.fail_next = 2
        r = self.call(session="e2e_retry")
        row = self.rows({r.gateway_request_id})[0]
        if self.stub:
            ok = row["attempts"] == 3 and row["status"] == "ok"
            return ("pass" if ok else "fail"), {"attempts": row["attempts"], "status": row["status"],
                                                "error": None if ok else "expected 2 refused tries then success"}
        retried = [x for x in self.rows() if x["attempts"] > 1 or x["failure_class"] == "provider_429"]
        return ("pass" if retried else "warn"), {
            "note": None if retried else "no 429 happened, so retries were not exercised live",
            "retried_rows": len(retried)}

    def check_shadow(self):
        if not (self.text and self.fallback):
            return "skip", {"note": "needs a second free model"}
        status, body = self.w.admin("POST", "/ai/routes", {"feature": "e2e_shadow", "capability": "text_routine",
                                                           "role": "shadow", "provider": "openrouter",
                                                           "model": self.fallback["id"], "shadow_pct": 100})
        assert status == 201, body
        r = self.call(feature="e2e_shadow", session="e2e_shadow_1")
        shadow = None
        for _ in range(60):
            rows = [x for x in self.rows() if x.get("shadow_of") == r.gateway_request_id]
            if rows:
                shadow = rows[0]
                break
            time.sleep(1)
        problems = []
        if not shadow:
            problems.append("no shadow row within 60 s")
        elif shadow["billing"] != "shadow" or shadow["charged_micro"] != 0:
            problems.append("shadow row was billed")
        elif shadow["status"] != "ok" and self.stub:
            problems.append(f"the shadow call failed: {shadow['failure_class']}")
        elif shadow["status"] != "ok":
            return "warn", {"note": f"shadow recorded but the free model failed: {shadow['failure_class']}"}
        served = self.rows({r.gateway_request_id})[0]
        if served["model"] != self.text["id"]:
            problems.append("the served call did not use rank 0")
        return ("fail" if problems else "pass"), {"error": "; ".join(problems) or None,
                                                  "shadow": shadow and {k: shadow[k] for k in (
                                                      "id", "model", "status", "cost_micro", "shadow_agree")}}

    def check_tool_use(self):
        if not self.text:
            return "skip", {"note": "no free text model"}
        if not self.text["tools"]:
            return "skip", {"note": f"{self.text['id']} does not support tools"}
        from plexora.agent.policy import Policy
        from plexora.ai.harness.conversations import ConversationStore
        from plexora.ai.harness.runner import AgentRunner

        runner = AgentRunner.create(ConversationStore(), gateway=self.client(), policy=Policy(), title="e2e")
        events = list(runner.turn("Call the list_skills tool, then tell me in one sentence how many skills "
                                  "there are."))
        kinds = [e.get("event") for e in events]
        calls = [e for e in events if e.get("event") == "tool_call"]
        results = [e for e in events if e.get("event") == "tool_result"]
        errors = [e for e in events if e.get("event") in ("error", "paused")]
        detail = {"events": kinds[-30:], "tools_called": [c.get("name") or c.get("tool") for c in calls],
                  "errors": errors[:3]}
        if errors:
            return "fail", {**detail, "error": str(errors[0])[:300]}
        if "done" not in kinds:
            return "fail", {**detail, "error": "the turn did not finish"}
        if not calls:
            return "warn", {**detail, "note": "the model answered without calling a tool"}
        if len(results) < len(calls):
            return "fail", {**detail, "error": "a tool call got no result"}
        return "pass", detail

    def check_gating(self):
        if not self.vision:
            return "skip", {"note": "no free vision model"}
        from plexora.agent import registry
        from plexora.ai import bench, bench_data
        from plexora.ai.harness.decision import GatingOptions, GatingRun

        registry.discover(["gating"])
        made = bench_data.register(self.data_root, "e2e_gating", scenario="easy", grid=16, size=512, seed=1,
                                   markers=MARKERS)
        if self.stub:
            self.stub.brain = bench.TruthAgent(made["values"], made["truth"]).answer
        return self._decision(GatingRun, GatingOptions(project="e2e_gating", markers=list(MARKERS), mode="propose",
                                                       max_packets=40, start_options={"reuse_answers": False}))

    def check_qc(self):
        if not self.vision:
            return "skip", {"note": "no free vision model"}
        from plexora.agent import registry
        from plexora.ai.harness.decision import QCOptions, QCRun
        from tests.qc_fixtures import make_qc_project
        from tests.test_qc_session import QCOracle

        registry.discover(["roi", "qc"])
        info = make_qc_project(self.data_root, name="e2e_qc", artifacts=("saturation", "fold"))
        holder = {}
        if self.stub:
            oracle = QCOracle(info)
            self.stub.brain = lambda packet: oracle.answer(packet, packet.get("session_id") or holder.get("session"))
        return self._decision(QCRun, QCOptions(project="e2e_qc", max_packets=60,
                                               start_options={"map_cell_um": 25.0, "reuse_answers": False}),
                              holder=holder)

    def _decision(self, runner, options, holder=None):
        """Run one decision session through the gateway and check the pipeline, not the answers."""
        from plexora import paths
        from plexora.agent import AgentSession
        from plexora.ai.harness.trace import TraceStore

        paths.reset()
        trace = TraceStore()
        holder = holder if holder is not None else {}

        def seen(event):
            if event.get("session_id"):
                holder["session"] = event["session_id"]

        summary = runner(options, gateway=self.client(), trace=trace, session=AgentSession(table_limit=4),
                         on_event=seen).run()
        calls = trace.calls(summary["run_id"])
        ids = {c["gateway_request_id"] for c in calls if c.get("gateway_request_id")}
        self.request_ids |= ids
        rows = {r["id"]: r for r in self.rows(ids)}
        problems = []
        if summary["status"] not in ("done", "waiting_for_user", "paused"):
            problems.append(f"session ended {summary['status']}: {summary.get('reason')}")
        if not calls:
            problems.append("no model calls")
        if len(rows) != len(ids):
            problems.append(f"{len(ids) - len(rows)} calls have no gateway row")
        mismatched = [c for c in calls if c.get("gateway_request_id") in rows and
                      (rows[c["gateway_request_id"]]["output_tokens"] != c["output_tokens"] or
                       rows[c["gateway_request_id"]]["cache_read"] != c["cache_read"])]
        if mismatched:
            problems.append(f"{len(mismatched)} calls' tokens differ between trace and gateway")
        if summary.get("gateway_run") and summary["status"] == "done":
            status, run = http("GET", f"{self.w.url}/v1/ai/runs/{summary['gateway_run']}",
                               headers={"Authorization": f"Bearer {self.tokens.get()}"})
            if run.get("status") != "finished":
                problems.append(f"gateway run left {run.get('status')}")
        detail = {"error": "; ".join(problems) or None, "status": summary["status"], "reason": summary.get("reason"),
                  "packets": summary["packets"], "model_calls": summary["model_calls"],
                  "invalid_answers": summary["invalid_answers"], "cache": summary["cache"],
                  "charged_micro": summary["charged_micro"], "units": summary.get("finish")}
        if problems:
            return "fail", detail
        if summary["invalid_answers"] and not self.stub:
            return "warn", {**detail, "note": f"{summary['invalid_answers']} answers failed validation (a free "
                                              "model's quality, not the pipeline)"}
        return "pass", detail

    def check_failover(self):
        if not (self.vision and self.fallback):
            return "skip", {"note": "needs two free vision models"}
        key = urllib.parse.quote(f"openrouter:{self.vision['id']}", safe="")
        status, body = self.w.admin("POST", f"/ai/providers/{key}/disable", {"reason": "e2e failover"})
        assert status == 200, body
        started_ms = int(time.time() * 1000) - 5000
        try:
            r = self.call(capability="vision_routine", session="e2e_failover")
        except Exception as exc:          # noqa: BLE001 -- told apart below: the fallback's quota, or the pipeline
            tried = [x for x in self.rows() if x["capability"] == "vision_routine" and x["failover"] == 1
                     and (x["started_at_ms"] or 0) >= started_ms]
            if tried and all(x["model"] == self.fallback["id"] for x in tried) and \
                    all(x["failure_class"] in ("provider_429", "circuit_open") for x in tried):
                return "warn", {"note": f"rank 1 was chosen (failover = 1 on {len(tried)} rows) but the free "
                                        f"model refused with 429: {exc}", "rows": len(tried)}
            raise
        finally:
            self.w.admin("POST", f"/ai/providers/{key}/enable")
        row = self.rows({r.gateway_request_id})[0]
        ok = row["model"] == self.fallback["id"] and row["failover"] == 1
        return ("pass" if ok else "fail"), {"served_by": row["model"], "failover": row["failover"],
                                            "error": None if ok else "rank 1 did not serve with the flag set"}

    def check_accounting(self):
        account = self.account
        bal = self.w.sql(f"SELECT * FROM ai_balances WHERE account_id = '{account}'")[0]
        ledger = self.w.sql(f"SELECT kind, bucket, SUM(amount_micro) AS micro FROM ai_ledger "
                            f"WHERE account_id = '{account}' GROUP BY kind, bucket")
        charged = self.w.sql(f"SELECT billing, COUNT(*) AS n, SUM(charged_micro) AS charged, SUM(cost_micro) AS cost,"
                             f" SUM(price_micro) AS price FROM ai_requests WHERE account_id = '{account}' "
                             f"GROUP BY billing")
        holds = self.w.sql(f"SELECT COUNT(*) AS n, COALESCE(SUM(micro), 0) AS micro FROM ai_holds "
                           f"WHERE account_id = '{account}'")[0]
        open_runs = self.w.sql(f"SELECT COUNT(*) AS n FROM ai_runs WHERE account_id = '{account}' "
                               f"AND status = 'open'")[0]["n"]
        total = sum(r["micro"] for r in ledger)
        settled = -sum(r["micro"] for r in ledger if r["kind"] == "settle")
        billed = sum(r["charged"] or 0 for r in charged if r["billing"] != "shadow")
        shadow_billed = sum(r["charged"] or 0 for r in charged if r["billing"] == "shadow")
        problems = []
        if total != bal["prepaid_micro"] + bal["allowance_micro"]:
            problems.append(f"ledger {total} != balance {bal['prepaid_micro'] + bal['allowance_micro']}")
        if settled != billed:
            problems.append(f"settlements {settled} != charges {billed}")
        if shadow_billed:
            problems.append(f"shadow calls charged {shadow_billed}")
        if bal["held_micro"] != holds["micro"]:
            problems.append(f"held {bal['held_micro']} != open holds {holds['micro']}")
        if open_runs == 0 and bal["held_micro"]:
            problems.append(f"{bal['held_micro']} micro still held with no open run")
        if billed <= 0:
            problems.append("nothing was charged (the nominal test price did not apply)")
        return ("fail" if problems else "pass"), {"error": "; ".join(problems) or None, "balance": bal,
                                                  "ledger": ledger, "requests": charged, "open_holds": holds,
                                                  "open_runs": open_runs}

    def calls_table(self) -> list[dict]:
        keep = ("id", "feature", "capability", "billing", "provider", "model", "route_id", "status", "failure_class",
                "attempts", "failover", "input_uncached", "cache_read", "cache_write_5m", "output_tokens",
                "cost_micro", "charged_micro", "shadow_of", "shadow_agree", "started_at_ms", "first_byte_ms",
                "finished_at_ms")
        with contextlib.suppress(Exception):
            return [{k: r.get(k) for k in keep} for r in reversed(self.rows())]
        return []


def markdown(report) -> str:
    lines = [f"# Plexora AI end-to-end pipeline check ({report['mode']})", "",
             f"{report['started']} to {report.get('finished')}. Models: "
             + ", ".join(f"{k} `{(v or {}).get('id')}`" for k, v in report["models"].items()), "",
             "| check | status | seconds | note |", "|---|---|---|---|"]
    for r in report["checks"]:
        d = r["detail"] if isinstance(r["detail"], dict) else {}
        note = (d.get("error") or d.get("note") or "").replace("|", "/")[:160]
        lines.append(f"| {r['check']} | {r['status'].upper()} | {r['seconds']} | {note} |")
    calls = report.get("calls") or []
    if calls:
        lines += ["", f"## Calls ({len(calls)})", "",
                  "| feature | capability | billing | model | status | tries | failover | in | cached | out | charged | ms |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for c in calls:
            ms = (c["finished_at_ms"] or 0) - (c["started_at_ms"] or 0)
            lines.append(f"| {c['feature']} | {c['capability']} | {c['billing']} | {c['model']} | {c['status']}"
                         f"{' ' + c['failure_class'] if c.get('failure_class') else ''} | {c['attempts']} | "
                         f"{c['failover']} | {c['input_uncached']} | {c['cache_read']} | {c['output_tokens']} | "
                         f"{c['charged_micro']} | {ms} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())

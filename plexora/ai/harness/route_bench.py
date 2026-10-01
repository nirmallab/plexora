"""`plexora ai route-bench`: Plexora's own routing bench for one candidate route.

A provider or an aggregator serves a module's traffic only after this bench
has shown, on Plexora's reference set, that the route meets the module's bar
(`licensing/src/ai/catalog.ts::BENCH`). An aggregator's own auto-router
optimises generic difficulty, cost and latency; the quality that matters here
is gate placement, which only Plexora can measure.

The run is real: the harness gates synthetic scenes with known phenotypes
(`plexora.ai.bench_data`) through the gateway's DEV route, naming the
candidate (`provider/model`), so the calls are billed at cost and tracked as
dev. Each image is scored the way `plexora ai bench gating` scores a session
(code agreement, per-marker F1), and the harness supplies the rest: invalid
answers, cache-read share, failures, cost and time per image.

`submit()` posts the result to `/admin/api/ai/evaluations` with an admin
token; the gateway decides `passed` from the metrics, never from this side.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request

import numpy as np

#: The bench version each module is on; mirrors catalog.ts BENCH (a test keeps them equal).
BENCH_VERSIONS = {"gating": "gating-1"}
DEFAULT_MARKERS = ("CD3", "CD8", "CD20", "CD4", "FOXP3")


def split_route(route: str) -> tuple[str, str]:
    provider, sep, model = route.partition("/")
    if not sep or not provider or not model:
        raise ValueError("Name the route as provider/model, e.g. openai/gpt-6.1-sol.")
    return provider, model


def bench_route(route: str, *, feature: str = "gating", capability: str = "vision_judgement",
                scenarios=("easy",), markers=None, gateway=None, seed: int = 0, grid: int = 24,
                size: int = 1024, units_per_worker: int = 1, on_event=None) -> dict:
    """Gate every scenario through `route`; returns the evaluation to submit."""
    from plexora import paths
    from plexora.agent import AgentSession, registry
    from plexora.ai import bench, bench_data
    from plexora.ai.harness.decision import GatingOptions, GatingRun
    from plexora.ai.harness.gateway import GatewayClient, GatewayError
    from plexora.plugins.gating.server.autogate import engine

    if feature not in BENCH_VERSIONS:
        raise ValueError(f"The route bench covers {', '.join(BENCH_VERSIONS)} so far.")
    provider, model = split_route(route)
    markers = tuple(markers or DEFAULT_MARKERS)
    gateway = gateway or GatewayClient(dev=True)
    if not gateway.dev:
        raise ValueError("The route bench runs on the dev route (an internal testing account).")
    rows, images = [], []
    # ignore_cleanup_errors: Windows can still hold a mask TIFF open as the bench ends.
    with tempfile.TemporaryDirectory(prefix="plexora-route-bench-", ignore_cleanup_errors=True) as root:
        previous = os.environ.get("PLEXORA_DATA_PATH")
        os.environ["PLEXORA_DATA_PATH"] = root
        paths.reset()
        try:
            registry.discover(["gating"])
            session = AgentSession(table_limit=4)
            for index, scenario in enumerate(scenarios):
                name = f"route_{scenario}"
                made = bench_data.register(root, name, scenario=scenario, grid=grid, size=size,
                                           seed=seed + index, markers=markers)
                options = GatingOptions(project=name, markers=list(markers), mode="propose",
                                        capability=capability, model=route,
                                        units_per_worker=units_per_worker,
                                        # The bench measures the route: no answer replayed from a memo.
                                        start_options={"reuse_answers": False})
                started = time.perf_counter()
                try:
                    summary = GatingRun(options, gateway=gateway, session=session, on_event=on_event).run()
                except GatewayError as exc:
                    summary = {"status": "failed", "reason": str(exc), "session_id": None}
                seconds = time.perf_counter() - started
                finals = {}
                if summary.get("session_id"):
                    record = engine.store().load(summary["session_id"])
                    finals = {u["marker"]: u.get("final") for u in record["units"].values()
                              if u["project"] == name}
                scored = bench.score_image(name, made["values"], made["truth"],
                                           {"route": {m: finals.get(m) for m in markers}}, {})
                for row in scored:
                    row["scenario"] = scenario
                rows.extend(scored)
                images.append({"scenario": scenario, "status": summary.get("status"),
                               "reason": summary.get("reason"), "packets": summary.get("packets", 0),
                               "model_calls": summary.get("model_calls", 0),
                               "invalid_answers": summary.get("invalid_answers", 0),
                               "tokens": summary.get("tokens") or {},
                               "charged_micro": summary.get("charged_micro", 0), "seconds": seconds})
            session.close()
        finally:
            if previous is None:
                os.environ.pop("PLEXORA_DATA_PATH", None)
            else:
                os.environ["PLEXORA_DATA_PATH"] = previous
            paths.reset()
    return {"feature": feature, "capability": capability, "provider": provider, "model": model,
            "bench_version": BENCH_VERSIONS[feature], "metrics": metrics(rows, images),
            "dataset_ids": [f"synthetic:{s}:seed{seed + i}:grid{grid}" for i, s in enumerate(scenarios)],
            "plexora_version": _version(), "images": images}


def metrics(rows: list, images: list) -> dict:
    """The numbers the gateway's bar is written in (catalog.ts BENCH)."""
    codes = [r["code_agreement"] for r in rows if r["marker"] == "*code*" and r.get("code_agreement") is not None]
    f1 = [r["f1"] for r in rows if not r["marker"].startswith("*") and r.get("f1") is not None]
    # A marker the route left ungated scores zero, not "missing": that is the failure being measured.
    f1 += [0.0 for r in rows if not r["marker"].startswith("*") and r.get("gate") is None]
    packets = sum(i["packets"] for i in images)
    calls = sum(i["model_calls"] for i in images)
    input_total = sum((i["tokens"] or {}).get("input", 0) for i in images)
    cache_read = sum((i["tokens"] or {}).get("cache_read", 0) for i in images)
    done = [i for i in images if i["status"] == "done"]
    return {
        "code_agreement": round(float(np.median(codes)), 4) if codes and len(done) == len(images) else 0.0,
        "marker_f1": round(float(np.median(f1)), 4) if f1 else 0.0,
        "invalid_answer_rate": round(sum(i["invalid_answers"] for i in images) / packets, 4) if packets else 1.0,
        "failure_rate": round(1 - len(done) / len(images), 4) if images else 1.0,
        "cache_hit_ratio": round(cache_read / input_total, 4) if input_total else 0.0,
        "bte_per_packet": None,
        "model_calls": calls,
        "packets": packets,
        "cost_micro_per_image": int(sum(i["charged_micro"] for i in images) / len(images)) if images else 0,
        "seconds_per_image": round(sum(i["seconds"] for i in images) / len(images), 1) if images else 0.0,
    }


def submit(evaluation: dict, *, admin_token: str, base_url: str | None = None) -> dict:
    """Record the evaluation on the gateway; returns {id, passed, misses, required}."""
    from plexora.ai.harness.gateway import gateway_url

    base = (base_url or gateway_url()).rstrip("/")
    body = {k: evaluation[k] for k in ("feature", "capability", "provider", "model", "bench_version", "metrics",
                                       "dataset_ids", "plexora_version")}
    body["metrics"] = {k: v for k, v in body["metrics"].items() if isinstance(v, (int, float))}
    request = urllib.request.Request(f"{base}/admin/api/ai/evaluations", method="POST",
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {admin_token}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"The gateway refused the evaluation: HTTP {exc.code} "
                           f"{exc.read(2000).decode('utf-8', 'replace')}") from None


def _version() -> str:
    try:
        from plexora.licensing import environment

        return environment.plexora_version()
    except Exception:                     # noqa: BLE001 -- a label, not a requirement
        return "unknown"

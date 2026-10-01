"""The local trace: what the harness did, call by call.

`<data_root>/.agent/ai/trace.sqlite`. Local and complete -- packet ids,
tokens, cache verdicts, credit, latency -- and linked to the gateway's
metadata row only by `gateway_request_id`. No prompt, image or answer text is
written here either; those stay in the session store the engine already keeps.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, kind TEXT NOT NULL, project TEXT, session_id TEXT, gateway_run_id TEXT,
  capability TEXT, billing TEXT, status TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
  summary_json TEXT);
CREATE TABLE IF NOT EXISTS model_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, worker INTEGER NOT NULL, seq INTEGER NOT NULL,
  packet_id TEXT, kind TEXT, capability TEXT, prefix_fp TEXT, verdict TEXT,
  input_uncached INTEGER, cache_read INTEGER, cache_write INTEGER, output_tokens INTEGER,
  price_micro INTEGER, charged_micro INTEGER, cost_micro INTEGER, gateway_request_id TEXT,
  latency_ms INTEGER, valid INTEGER, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS model_calls_run ON model_calls(run_id, id);
CREATE TABLE IF NOT EXISTS tool_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, agent TEXT, tool TEXT NOT NULL,
  capability TEXT, permission TEXT, source TEXT NOT NULL, ok INTEGER, offloaded INTEGER,
  operation_id TEXT, latency_ms INTEGER, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS tool_calls_run ON tool_calls(run_id, id);
CREATE TABLE IF NOT EXISTS tasks (
  run_id TEXT NOT NULL, task_id TEXT NOT NULL, parent TEXT, label TEXT, state TEXT NOT NULL,
  started_at REAL, finished_at REAL, detail_json TEXT, PRIMARY KEY (run_id, task_id));
"""


def default_path() -> Path:
    from plexora import paths

    return Path(paths.data_root()) / ".agent" / "ai" / "trace.sqlite"


class TraceStore:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _write(self, sql: str, params=()):
        with self._lock, self._connect() as db:
            db.execute(sql, params)

    def start_run(self, run_id: str, kind: str, **fields) -> None:
        self._write("INSERT OR REPLACE INTO runs (run_id, kind, project, session_id, gateway_run_id, capability, "
                    "billing, status, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?)",
                    (run_id, kind, fields.get("project"), fields.get("session_id"), fields.get("gateway_run_id"),
                     fields.get("capability"), fields.get("billing"), time.time()))

    def update_run(self, run_id: str, **fields) -> None:
        if "summary" in fields:
            fields["summary_json"] = json.dumps(fields.pop("summary"), default=str)
        if not fields:
            return
        names = ", ".join(f"{name} = ?" for name in fields)
        self._write(f"UPDATE runs SET {names} WHERE run_id = ?", (*fields.values(), run_id))

    def finish_run(self, run_id: str, status: str, summary: dict) -> None:
        self.update_run(run_id, status=status, finished_at=time.time(), summary=summary)

    def call(self, run_id: str, **f) -> None:
        self._write(
            "INSERT INTO model_calls (run_id, worker, seq, packet_id, kind, capability, prefix_fp, verdict, "
            "input_uncached, cache_read, cache_write, output_tokens, price_micro, charged_micro, cost_micro, "
            "gateway_request_id, latency_ms, valid, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, f.get("worker", 0), f.get("seq", 0), f.get("packet_id"), f.get("kind"), f.get("capability"),
             f.get("prefix_fp"), f.get("verdict"), f.get("input_uncached", 0), f.get("cache_read", 0),
             f.get("cache_write", 0), f.get("output_tokens", 0), f.get("price_micro", 0), f.get("charged_micro", 0),
             f.get("cost_micro"), f.get("gateway_request_id"), f.get("latency_ms", 0),
             None if f.get("valid") is None else int(bool(f.get("valid"))), time.time()))

    def tool_call(self, run_id: str, **f) -> None:
        """One tool call of a conversation. `source` is `live`, `cache` (the
        tool-result cache answered), `local` (a harness tool) or `declined`."""
        self._write(
            "INSERT INTO tool_calls (run_id, agent, tool, capability, permission, source, ok, offloaded, "
            "operation_id, latency_ms, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, f.get("agent"), f.get("tool"), f.get("capability"), f.get("permission"),
             f.get("source", "live"), None if f.get("ok") is None else int(bool(f.get("ok"))),
             int(bool(f.get("offloaded"))), f.get("operation_id"), f.get("latency_ms", 0), time.time()))

    def tool_calls(self, run_id: str) -> list[dict]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM tool_calls WHERE run_id = ? ORDER BY id",
                                                (run_id,))]

    def task(self, run_id: str, task_id: str, state: str, **f) -> None:
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO tasks (run_id, task_id, parent, label, state, started_at, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(run_id, task_id) DO UPDATE SET state = excluded.state, "
                "finished_at = CASE WHEN excluded.state IN ('done', 'failed', 'cancelled', 'skipped') THEN ? "
                "ELSE finished_at END, detail_json = COALESCE(excluded.detail_json, detail_json)",
                (run_id, task_id, f.get("parent"), f.get("label"), state, now,
                 json.dumps(f["detail"], default=str) if f.get("detail") is not None else None, now))

    # -- reading ---------------------------------------------------------------

    def runs(self, limit: int = 20) -> list[dict]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))]

    def run(self, run_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id = ? OR run_id LIKE ? ORDER BY started_at DESC",
                             (run_id, f"{run_id}%")).fetchone()
            return dict(row) if row else None

    def calls(self, run_id: str) -> list[dict]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM model_calls WHERE run_id = ? ORDER BY id", (run_id,))]

    def tasks(self, run_id: str) -> list[dict]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM tasks WHERE run_id = ? ORDER BY started_at",
                                                (run_id,))]

    def cache_report(self, run_id: str) -> dict:
        calls = self.calls(run_id)
        read = sum(c["cache_read"] or 0 for c in calls)
        total = sum((c["input_uncached"] or 0) + (c["cache_read"] or 0) + (c["cache_write"] or 0) for c in calls)
        verdicts: dict = {}
        for c in calls:
            verdicts[c["verdict"]] = verdicts.get(c["verdict"], 0) + 1
        workers = len({c["worker"] for c in calls})
        return {"calls": len(calls), "workers": workers, "verdicts": verdicts,
                "cache_read_share": round(read / total, 4) if total else 0.0,
                "prefixes": sorted({c["prefix_fp"] for c in calls if c["prefix_fp"]}),
                "input_tokens": total, "output_tokens": sum(c["output_tokens"] or 0 for c in calls),
                "charged_micro": sum(c["charged_micro"] or 0 for c in calls),
                "invalid_answers": sum(1 for c in calls if c["valid"] == 0),
                "tool_calls": self._tool_sources(run_id)}

    def _tool_sources(self, run_id: str) -> dict:
        out: dict = {}
        for row in self.tool_calls(run_id):
            out[row["source"]] = out.get(row["source"], 0) + 1
        return out

#!/usr/bin/env python
"""A recording pass-through proxy for model calls: one instrument for every arm.

    python tools/bench_proxy.py --upstream https://api.anthropic.com --out DIR [--port 0]

Point a client at it (Claude Code: ANTHROPIC_BASE_URL; Plexora's harness:
PLEXORA_AI_GATEWAY) and it forwards every request unchanged, streams the
answer back unchanged, and writes one JSON line per HTTP call to
`DIR/calls.jsonl`:

  t_start / t_headers / t_first_token / t_end   wall-clock seconds
  path, status, model, task (the gateway's), stream
  request: system/tool/message counts and characters, `cache_control`
           positions, effort/thinking, max_tokens, images (sha, bytes,
           pixel size, estimated tokens)
  usage:   input_uncached, cache_read, cache_write_5m, cache_write_1h, output
  retry:   the SDK's retry count header, or a repeated idempotency key / body

The whole request body is kept too (`DIR/bodies/<n>.json.gz`), every image
replaced by `{"image_sha": ...}` and stored once under `DIR/images/`: exactly
what each arm sent, for the report's input comparison. Authorization headers
are never written. Prints `PORT <n>` once it listens. Standard library only
(Pillow, when present, reads image sizes).

A dev tool for docs/internal/bench (tools/bench_harness_vs_cc.py starts it).
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import http.client
import io
import json
import math
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}


def image_size(raw: bytes):
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            return im.size
    except Exception:            # noqa: BLE001 -- no Pillow, or not an image it knows
        return None


def image_tokens(size) -> int | None:
    """Anthropic's estimate: a long side over 1568 px (or more than ~1.15 MP)
    is scaled down first, then width*height/750."""
    if not size:
        return None
    w, h = size
    scale = min(1.0, 1568 / max(w, h), math.sqrt(1_150_000 / (w * h)) if w * h else 1.0)
    return int(math.ceil((w * scale) * (h * scale) / 750))


class Recorder:
    def __init__(self, out: Path):
        self.out = out
        (out / "bodies").mkdir(parents=True, exist_ok=True)
        (out / "images").mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.n = 0
        self.seen_bodies: set = set()
        self.seen_keys: set = set()
        self.images: dict = {}

    def next_id(self) -> int:
        with self.lock:
            self.n += 1
            return self.n

    def write(self, row: dict) -> None:
        line = json.dumps(row, separators=(",", ":"), default=str)
        with self.lock, open(self.out / "calls.jsonl", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # -- request ---------------------------------------------------------------------

    def _image(self, source: dict) -> dict:
        data = source.get("data") or ""
        raw = base64.b64decode(data) if data else b""
        sha = hashlib.sha1(raw).hexdigest()[:16]
        with self.lock:
            known = self.images.get(sha)
        if known is None:
            size = image_size(raw)
            known = {"image_sha": sha, "bytes": len(raw), "media_type": source.get("media_type"),
                     "px": list(size) if size else None, "est_tokens": image_tokens(size)}
            ext = (source.get("media_type") or "image/bin").split("/")[-1]
            path = self.out / "images" / f"{sha}.{ext}"
            if not path.exists():
                path.write_bytes(raw)
            with self.lock:
                self.images[sha] = known
        return dict(known)

    def _strip(self, node, images: list, marks: list, where: str):
        """The body with images swapped for references; collects images and
        cache_control positions on the way."""
        if isinstance(node, dict):
            if node.get("type") == "image" and isinstance(node.get("source"), dict) \
                    and node["source"].get("type") == "base64":
                ref = self._image(node["source"])
                images.append({**ref, "at": where})
                return {"type": "image", "image_ref": ref["image_sha"]}
            if "cache_control" in node:
                marks.append({"at": where, **(node["cache_control"] or {})})
            return {k: self._strip(v, images, marks, f"{where}.{k}") for k, v in node.items()}
        if isinstance(node, list):
            return [self._strip(v, images, marks, f"{where}[{i}]") for i, v in enumerate(node)]
        return node

    def request(self, n: int, body: bytes, headers) -> dict:
        try:
            parsed = json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if not isinstance(parsed, dict):
            return {"body_bytes": len(body)}
        images: list = []
        marks: list = []
        stripped = self._strip(parsed, images, marks, "$")
        with gzip.open(self.out / "bodies" / f"{n:05d}.json.gz", "wt", encoding="utf-8") as handle:
            json.dump(stripped, handle)
        req = parsed.get("request") if isinstance(parsed.get("request"), dict) else parsed
        system = req.get("system")
        system_chars = (len(system) if isinstance(system, str) else
                        sum(len(b.get("text") or "") for b in system or () if isinstance(b, dict)))
        messages = req.get("messages") or []
        message_chars = len(json.dumps(self._strip(messages, [], [], "m")))
        tools = req.get("tools") or []
        digest = hashlib.sha1(body).hexdigest()
        key = headers.get("Idempotency-Key")
        with self.lock:
            repeat_body = digest in self.seen_bodies
            repeat_key = bool(key) and key in self.seen_keys
            self.seen_bodies.add(digest)
            if key:
                self.seen_keys.add(key)
        return {
            "model": parsed.get("model"), "task": parsed.get("task"),
            "capability": parsed.get("capability"), "stream": parsed.get("stream"),
            "max_tokens": req.get("max_tokens"), "thinking": req.get("thinking"),
            "effort": (req.get("output_config") or {}).get("effort"),
            "output_schema": bool(req.get("output_schema") or (req.get("output_config") or {}).get("format")),
            "system_chars": system_chars, "n_messages": len(messages), "message_chars": message_chars,
            "n_tools": len(tools), "tool_names": [t.get("name") for t in tools if isinstance(t, dict)],
            "tools_chars": len(json.dumps(tools)), "cache_control": marks,
            "images": images, "n_images": len(images),
            "image_bytes": sum(i["bytes"] for i in images),
            "image_est_tokens": sum(i.get("est_tokens") or 0 for i in images),
            "body_sha": digest[:16], "body_bytes": len(body),
            "retry_header": headers.get("x-stainless-retry-count"),
            "repeat_body": repeat_body, "repeat_key": repeat_key,
        }


class SSEWatch:
    """Reads the answer as it passes: usage, model, first token, stop reason."""

    def __init__(self):
        self.buffer = b""
        self.event = None
        self.usage: dict = {}
        self.plexora: dict = {}
        self.model = None
        self.stop = None
        self.t_first_token = None
        self.error = None

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            self._line(line.decode("utf-8", "replace").rstrip("\r"))

    def _line(self, line: str) -> None:
        if line.startswith("event:"):
            self.event = line[6:].strip()
            return
        if not line.startswith("data:"):
            return
        try:
            data = json.loads(line[5:].strip())
        except ValueError:
            return
        kind = self.event or (data.get("type") if isinstance(data, dict) else None)
        if not isinstance(data, dict):
            return
        if kind == "message_start":
            message = data.get("message") or {}
            self.model = message.get("model") or self.model
            self._usage(message.get("usage") or {})
        elif kind == "message_delta":
            self._usage(data.get("usage") or {})
            self.stop = (data.get("delta") or {}).get("stop_reason") or self.stop
        elif kind == "content_block_delta" and self.t_first_token is None:
            self.t_first_token = time.time()
        elif kind == "plexora.usage":
            self.plexora = data
        elif kind in ("error", "plexora.error"):
            self.error = data

    def _usage(self, raw: dict) -> None:
        for key, value in raw.items():
            if isinstance(value, (int, float)) and value:
                self.usage[key] = value
            elif isinstance(value, dict):
                for sub, inner in value.items():
                    if isinstance(inner, (int, float)) and inner:
                        self.usage[f"{key}.{sub}"] = inner


def normal_usage(watch: SSEWatch, body_json) -> dict:
    """One usage shape for both upstreams."""
    if watch.plexora.get("usage"):
        u = watch.plexora["usage"]
        return {"input_uncached": int(u.get("input_uncached") or 0), "cache_read": int(u.get("cache_read") or 0),
                "cache_write_5m": int(u.get("cache_write_5m") or 0),
                "cache_write_1h": int(u.get("cache_write_1h") or 0),
                "output": int(u.get("output_tokens") or 0)}
    u = dict(watch.usage)
    if not u and isinstance(body_json, dict) and isinstance(body_json.get("usage"), dict):
        u = body_json["usage"]
        u = {**u, **{f"cache_creation.{k}": v for k, v in (u.get("cache_creation") or {}).items()}}
    if not u:
        return {}
    write = int(u.get("cache_creation_input_tokens") or 0)
    w1h = int(u.get("cache_creation.ephemeral_1h_input_tokens") or 0)
    w5m = int(u.get("cache_creation.ephemeral_5m_input_tokens") or 0)
    if write and not (w1h or w5m):
        w5m = write
    return {"input_uncached": int(u.get("input_tokens") or 0), "cache_read": int(u.get("cache_read_input_tokens") or 0),
            "cache_write_5m": w5m, "cache_write_1h": w1h, "output": int(u.get("output_tokens") or 0)}


def make_handler(upstream: str, recorder: Recorder):
    target = urllib.parse.urlsplit(upstream)
    base_path = target.path.rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _proxy(self):
            n = recorder.next_id()
            t_start = time.time()
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            row = {"n": n, "t_start": t_start, "method": self.command, "path": self.path}
            if self.command == "POST":
                row["request"] = recorder.request(n, body, self.headers)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            connection = (http.client.HTTPSConnection if target.scheme == "https"
                          else http.client.HTTPConnection)(target.netloc, timeout=600)
            try:
                connection.request(self.command, base_path + self.path, body=body or None, headers=headers)
                response = connection.getresponse()
            except Exception as exc:          # noqa: BLE001 -- recorded, then a 502
                row.update(status=502, error=f"{type(exc).__name__}: {exc}", t_end=time.time())
                recorder.write(row)
                self.send_error(502, "bench proxy: upstream unreachable")
                return
            row["status"] = response.status
            row["t_headers"] = time.time()
            row["request_id"] = response.getheader("request-id") or response.getheader("x-request-id")
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in HOP:
                    self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            watch = SSEWatch()
            streamed = "event-stream" in (response.getheader("Content-Type") or "")
            kept = bytearray()
            try:
                while True:
                    chunk = response.read1(65536) if hasattr(response, "read1") else response.read(65536)
                    if not chunk:
                        break
                    if streamed:
                        watch.feed(chunk)
                    elif len(kept) < 4 * 1024 * 1024:
                        kept.extend(chunk)
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError) as exc:
                row["client_gone"] = type(exc).__name__
            finally:
                connection.close()
            row["t_end"] = time.time()
            body_json = None
            if kept:
                try:
                    body_json = json.loads(bytes(kept).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    body_json = None
            row["t_first_token"] = watch.t_first_token
            final = body_json if isinstance(body_json, dict) else {}
            row["served_model"] = watch.plexora.get("model") or watch.model or final.get("model")
            row["provider"] = watch.plexora.get("provider")
            row["stop_reason"] = watch.stop or final.get("stop_reason")
            row["usage"] = normal_usage(watch, body_json)
            if watch.plexora:
                row["gateway"] = {k: watch.plexora.get(k) for k in
                                  ("price_micro", "charged_micro", "cost_micro", "gateway_request_id", "status")}
            if watch.error or (response.status >= 400 and final):
                row["error"] = watch.error or final.get("error")
            recorder.write(row)

        do_POST = _proxy
        do_GET = _proxy
        do_PUT = _proxy
        do_DELETE = _proxy
        do_HEAD = _proxy

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.upstream, Recorder(out)))
    server.daemon_threads = True
    print(f"PORT {server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

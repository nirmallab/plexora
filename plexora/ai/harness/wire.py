"""The harness's side of the gateway wire: the envelope it sends, what comes
back, and the canonical JSON every cached byte is built from."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field


def canonical(value) -> str:
    """Sorted keys, no whitespace: the same object always gives the same bytes,
    which is what keeps a prompt-cache prefix warm across workers and sessions."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def text_block(text: str, *, cache: bool = False) -> dict:
    block = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def image_block(data: bytes, fmt: str = "webp") -> dict:
    media = {"webp": "image/webp", "png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg"}
    return {"type": "image", "source": {"type": "base64", "media_type": media.get(fmt, "image/png"),
                                        "data": base64.b64encode(data).decode("ascii")}}


@dataclass
class Usage:
    input_uncached: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    output_tokens: int = 0

    @classmethod
    def of(cls, raw) -> "Usage":
        raw = raw if isinstance(raw, dict) else {}
        return cls(**{name: int(raw.get(name) or 0) for name in cls.__dataclass_fields__})

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def input_total(self) -> int:
        return self.input_uncached + self.cache_read + self.cache_write

    def bte(self) -> float:
        """Base-input-token equivalents (reads 0.1x, 5-minute writes 1.25x, 1-hour
        writes 2x, output 5x): one number to compare runs by, whatever the model."""
        return (self.input_uncached + 0.1 * self.cache_read + 1.25 * self.cache_write_5m
                + 2 * self.cache_write_1h + 5 * self.output_tokens)


@dataclass
class ModelRequest:
    capability: str
    system: list
    messages: list
    max_tokens: int = 4096
    output_schema: dict | None = None
    context: dict = field(default_factory=dict)
    model: str | None = None          # dev route only

    def envelope(self) -> dict:
        request = {"system": self.system, "messages": self.messages, "max_tokens": self.max_tokens}
        if self.output_schema is not None:
            request["output_schema"] = self.output_schema
        body = {"capability": self.capability, "context": self.context, "request": request}
        if self.model:
            body["model"] = self.model
        return body


@dataclass
class ModelResponse:
    text: str
    stop_reason: str | None
    usage: Usage
    gateway_request_id: str | None = None
    status: str = "ok"
    price_micro: int = 0
    charged_micro: int = 0
    cost_micro: int | None = None
    billing: str = "credits"
    model: str | None = None
    balance: dict = field(default_factory=dict)
    run: dict | None = None
    latency_ms: int = 0

    def json(self):
        """The answer as JSON: the whole text, or the first object in it."""
        text = self.text.strip()
        try:
            return json.loads(text)
        except ValueError:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start:end + 1])
            raise

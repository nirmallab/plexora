"""The harness's side of the gateway wire: the envelope it sends, what comes
back, and the canonical JSON every cached byte is built from."""

from __future__ import annotations

import base64
import json
import re
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


def with_breakpoints(messages: list) -> list:
    """The conversation as sent: the last block of the newest message, and of
    the newest earlier user message, carry a cache breakpoint.

    The system prompt carries the first breakpoint (prefix.py); these two make
    each call read everything up to its previous turn from cache and write only
    the new turn. Marking the previous turn as well means a call reads exactly
    what the last one wrote, however many image blocks a packet adds (a
    provider looks back only so far from a breakpoint). Three in all, under
    Anthropic's four. A copy: the stored history is never marked, so its bytes
    -- and a saved chat -- stay as they were."""
    out = list(messages)
    marks = [len(out) - 1] if out else []
    earlier = next((i for i in range(len(out) - 2, -1, -1) if out[i].get("role") == "user"), None)
    if earlier is not None:
        marks.append(earlier)
    for i in marks:
        content = out[i].get("content")
        if not isinstance(content, list) or not content:
            continue        # a plain string stays as it is: its bytes must not change
        out[i] = {**out[i], "content": content[:-1] + [{**content[-1], "cache_control": {"type": "ephemeral"}}]}
    return out


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
    #: Tool definitions (chat mode); decision mode sends none.
    tools: list | None = None
    #: The task this call does (`gating.threshold_evaluation`, plexora/ai/tasks.py): the gateway serves
    #: each task with the model an administrator assigned to it. None leaves it to the module's default.
    task: str | None = None

    def envelope(self) -> dict:
        request = {"system": self.system, "messages": self.messages, "max_tokens": self.max_tokens}
        if self.tools:
            request["tools"] = self.tools
        if self.output_schema is not None:
            request["output_schema"] = self.output_schema
        body = {"capability": self.capability, "context": self.context, "request": request}
        if self.task:
            body["task"] = self.task
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
    #: The approved model that served the call, and through which provider: the gateway's names, which
    #: the package never chooses or interprets.
    model: str | None = None
    provider: str | None = None
    balance: dict = field(default_factory=dict)
    run: dict | None = None
    latency_ms: int = 0
    #: The answer's content blocks in order: `text` and `tool_use` (with its
    #: parsed `input`). Thinking blocks are not kept: the gateway accepts only
    #: text, image, tool_use and tool_result blocks back.
    blocks: list = field(default_factory=list)

    @property
    def tool_uses(self) -> list:
        return [b for b in self.blocks if b.get("type") == "tool_use"]

    def json(self):
        """The answer as JSON: the whole text, or the first object in it.

        Weaker models wrap the object in a code fence or prose, and reasoning
        models put `<think>...</think>` first; both are stripped here and the
        answer is still validated against its model afterwards."""
        text = re.sub(r"<think>.*?</think>", "", self.text, flags=re.S).strip()
        try:
            return json.loads(text)
        except ValueError:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                return json.loads(text[start:end + 1])
            raise

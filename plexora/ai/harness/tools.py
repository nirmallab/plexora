"""The conversational agent's tools: the capability registry, deferred.

`ToolAdapter` turns `registry.describe()` into what a model is given, and
runs what it calls.

**Frozen at conversation start.** The catalog is every capability the policy
could let through (egress allowed; writes only when the policy allows writes;
source-file writes and deletes included, because they pause for the user's
approval rather than being refused), minus the `ai.*` chat capabilities
themselves and, with no viewer to drive, viewer capabilities. Sorted by tool
name, so the bytes are the same for every conversation of one build and
policy.

**Deferred schemas.** The model is not sent ~100 full tool schemas. The
system prompt lists each catalog tool's name and one-line purpose; the
`tools` array holds only the local tools (`load_tool`, `list_skills`,
`read_skill`, `read_artifact`, the sub-agent and board tools). `load_tool`
APPENDS the full definitions it is asked for to the end of the `tools` array:
nothing before them is reordered or changed, so every earlier byte of the
prefix is identical (`tests/test_ai_chat.py` pins the fingerprint). An append
does cost one cache write of what follows the tools (the system prompt and
history) on the next call -- load several at once.

**Executing.** A registry tool runs through `registry.invoke`, through the
`ToolResultCache` for cacheable reads (keyed on the project's revision);
images go back as image blocks (`plexora.mcp.tools.split_images`), text is
compact JSON, and a result above the offload threshold becomes a stub the
model reads slices of with `read_artifact` (`offload.py`). A reversible write
reports its receipt's operation_id so the panel can offer Undo.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field

from plexora.ai.harness import offload as offloading
from plexora.ai.harness.wire import canonical, image_block, text_block

#: The Worker accepts at most 64 tool definitions per request.
MAX_TOOLS = 64
NEEDS_APPROVAL = ("source_file_write", "destructive")
#: Images one tool result may carry to the model.
MAX_RESULT_IMAGES = 4

LOAD_TOOL = "load_tool"
SPAWN_AGENTS = "spawn_agents"
AWAIT_AGENTS = "await_agents"
READ_BOARD = "read_board"
POST_BOARD = "post_board"
LIST_SKILLS = "list_skills"
READ_SKILL = "read_skill"
READ_ARTIFACT = "read_artifact"

#: Local tools a sub-agent always has (it never spawns or loads beyond its brief).
SUBAGENT_LOCAL = (LOAD_TOOL, LIST_SKILLS, READ_SKILL, READ_ARTIFACT, READ_BOARD, POST_BOARD)


def _local_definitions() -> list:
    defs = [
        {"name": LOAD_TOOL,
         "description": "Load the full definitions of catalog tools so you can call them. Pass every "
                        "name you expect to need; loaded tools stay available for the conversation.",
         "input_schema": {"type": "object", "properties": {
             "names": {"type": "array", "items": {"type": "string"},
                       "description": "Tool names from the catalog."}}, "required": ["names"]}},
        {"name": LIST_SKILLS,
         "description": "Plexora's runtime skills: name, title and when to use each.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": READ_SKILL,
         "description": "Read one skill (a SKILL.md) before doing that kind of work.",
         "input_schema": {"type": "object", "properties": {"name": {"type": "string"}},
                          "required": ["name"]}},
        {"name": READ_ARTIFACT,
         "description": "Read part of a large tool result that was offloaded (it came back as a stub "
                        "with an artifact_id): a character range, or the lines matching a query.",
         "input_schema": {"type": "object", "properties": {
             "artifact_id": {"type": "string"},
             "start": {"type": "integer", "description": "First character (default 0)."},
             "end": {"type": "integer", "description": "One past the last character."},
             "query": {"type": "string", "description": "Return the lines containing this text."}},
             "required": ["artifact_id"]}},
        {"name": SPAWN_AGENTS,
         "description": "Run sub-agents for independent pieces of work, in parallel where their "
                        "dependencies allow. Each gets a brief and returns a short typed summary. "
                        "wait=false returns their ids at once; await_agents collects the summaries.",
         "input_schema": {"type": "object", "properties": {
             "agents": {"type": "array", "items": {"type": "object", "properties": {
                 "id": {"type": "string", "description": "Optional id other agents can depend on."},
                 "role": {"type": "string", "description": "A short role, e.g. image_reader."},
                 "brief": {"type": "string", "description": "Exactly what this agent must do."},
                 "inputs": {"type": "object", "description": "Small JSON inputs (names, ids)."},
                 "depends_on": {"type": "array", "items": {"type": "string"}},
                 "tools": {"type": "array", "items": {"type": "string"},
                           "description": "Catalog tools it may call (default: the read-only ones)."}},
                 "required": ["role", "brief"]}},
             "wait": {"type": "boolean", "description": "Wait for every summary (default true)."}},
             "required": ["agents"]}},
        {"name": AWAIT_AGENTS,
         "description": "Wait for sub-agents started with wait=false and return their summaries.",
         "input_schema": {"type": "object", "properties": {
             "ids": {"type": "array", "items": {"type": "string"}},
             "timeout_s": {"type": "number", "description": "At most this long (default 120)."}},
             "required": ["ids"]}},
        {"name": READ_BOARD,
         "description": "Read facts the agents of this conversation posted. A key ending in * reads "
                        "every key with that prefix.",
         "input_schema": {"type": "object", "properties": {
             "keys": {"type": "array", "items": {"type": "string"}}}, "required": ["keys"]}},
        {"name": POST_BOARD,
         "description": "Post a small fact for the other agents of this conversation to read.",
         "input_schema": {"type": "object", "properties": {
             "key": {"type": "string"}, "value": {"description": "Any small JSON value."}},
             "required": ["key", "value"]}},
    ]
    return sorted(defs, key=lambda d: d["name"])


LOCAL_NAMES = tuple(d["name"] for d in _local_definitions())


def one_line(purpose: str, limit: int = 140) -> str:
    text = " ".join(str(purpose or "").split())
    for stop in (". ", "; "):
        if stop in text:
            text = text.split(stop, 1)[0]
            break
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def build_catalog(policy, *, viewer: bool = False) -> list:
    """The capabilities a conversation may use under `policy`, by tool name."""
    from plexora.agent import registry

    entries = []
    for cap in registry.all_capabilities():
        if cap.name.startswith("ai.") or cap.tool_name in LOCAL_NAMES:
            continue
        if cap.egress not in policy.egress:
            continue
        if cap.permission != "read" and not policy.allow_writes:
            continue
        if cap.viewer_required and not viewer:
            continue
        entries.append({"tool": cap.tool_name, "capability": cap.name, "permission": cap.permission,
                        "purpose": one_line(cap.purpose)})
    return sorted(entries, key=lambda e: e["tool"])


def catalog_text(catalog: list) -> str:
    marks = {"read": "", "reversible_write": " [write, undoable]",
             "source_file_write": " [writes source file: needs approval]",
             "destructive": " [cannot be undone: needs approval]"}
    lines = ["TOOL CATALOG (call load_tool with a name before calling it)"]
    lines += [f"- {e['tool']}: {e['purpose']}{marks.get(e['permission'], '')}" for e in catalog]
    return "\n".join(lines)


def definition_for(capability) -> dict:
    from plexora.mcp.tools import description_for

    schema = capability.input_model.model_json_schema() if capability.input_model else {}
    schema = _strip_titles(copy.deepcopy(schema))
    schema.setdefault("type", "object")
    return {"name": capability.tool_name, "description": description_for(capability), "input_schema": schema}


def _strip_titles(node):
    if isinstance(node, dict):
        props = node.get("properties")
        out = {k: _strip_titles(v) for k, v in node.items() if k != "title" or isinstance(v, dict)}
        if isinstance(props, dict):
            out["properties"] = {k: _strip_titles(v) for k, v in props.items()}
        return out
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


@dataclass
class ToolOutcome:
    tool_use_id: str
    name: str
    content: list
    is_error: bool = False
    source: str = "live"
    capability: str | None = None
    permission: str | None = None
    operation_id: str | None = None
    undo: bool = False
    offloaded: str | None = None
    images: list = field(default_factory=list)
    summary: str = ""
    latency_ms: int = 0
    result: object = None

    def block(self) -> dict:
        block = {"type": "tool_result", "tool_use_id": self.tool_use_id, "content": self.content}
        if self.is_error:
            block["is_error"] = True
        return block


def error_outcome(tool_use_id: str, name: str, message: str, **fields) -> ToolOutcome:
    return ToolOutcome(tool_use_id, name, [text_block(message)], is_error=True, summary=message, **fields)


class ToolAdapter:
    """The tools of one conversation. Thread-safe: sub-agents share it."""

    def __init__(self, session, *, policy, catalog: list | None = None, loaded: list | None = None,
                 cache=None, link=None, notify=None, audit=None, viewer: bool = False,
                 offload_tokens: int = 4000, offload_root=None):
        self.session = session
        self.policy = policy
        self.catalog = list(catalog) if catalog is not None else build_catalog(policy, viewer=viewer)
        self.by_tool = {e["tool"]: e for e in self.catalog}
        self.cache = cache
        self.link = link
        self.notify = notify
        self.audit = audit
        self.offload_tokens = offload_tokens
        self.offload_root = offload_root
        self.local = _local_definitions()
        self._loaded: list[dict] = []
        self._lock = threading.Lock()
        for name in loaded or ():
            self._append(name)

    # -- what the model is sent ---------------------------------------------------

    @property
    def loaded(self) -> list:
        with self._lock:
            return [d["name"] for d in self._loaded]

    def definitions(self) -> list:
        """Local tools (sorted), then loaded definitions in load order."""
        with self._lock:
            return list(self.local) + list(self._loaded)

    def catalog_text(self) -> str:
        return catalog_text(self.catalog)

    def _append(self, name: str) -> str:
        from plexora.agent import registry

        if name in LOCAL_NAMES:
            return "local"
        entry = self.by_tool.get(name)
        if entry is None:
            return "unknown"
        if any(d["name"] == name for d in self._loaded):
            return "already"
        if len(self.local) + len(self._loaded) >= MAX_TOOLS:
            return "full"
        self._loaded.append(definition_for(registry.get(entry["capability"])))
        return "loaded"

    def load(self, names) -> dict:
        out: dict = {"loaded": [], "already": [], "unknown": [], "local": [], "full": []}
        with self._lock:
            for name in [str(n) for n in (names or [])]:
                out[self._append(name)].append(name)
        result = {k: v for k, v in out.items() if v}
        if out["unknown"]:
            result["hint"] = "only names in the tool catalog can be loaded"
        if out["full"]:
            result["hint"] = f"at most {MAX_TOOLS} tools can be loaded in one conversation"
        return result

    # -- running a call -----------------------------------------------------------------

    def capability(self, tool: str):
        from plexora.agent import registry

        entry = self.by_tool.get(tool)
        return registry.get(entry["capability"]) if entry else None

    def needs_approval(self, tool: str) -> bool:
        cap = self.capability(tool)
        return cap is not None and cap.permission in NEEDS_APPROVAL

    def execute(self, tool_use_id: str, name: str, arguments: dict, *, policy=None,
                allowed: set | None = None) -> ToolOutcome:
        started = time.monotonic()
        arguments = dict(arguments or {})
        if name in LOCAL_NAMES:
            outcome = self._local(tool_use_id, name, arguments)
        elif name not in self.by_tool:
            outcome = error_outcome(tool_use_id, name, f"{name!r} is not a tool in this conversation's "
                                    "catalog.", source="local")
        elif allowed is not None and name not in allowed:
            outcome = error_outcome(tool_use_id, name, f"{name} is not one of the tools this sub-agent "
                                    "was given.", source="local")
        else:
            outcome = self._registry(tool_use_id, name, arguments, policy or self.policy)
        outcome.latency_ms = int((time.monotonic() - started) * 1000)
        return outcome

    def _local(self, tool_use_id: str, name: str, arguments: dict) -> ToolOutcome:
        from plexora.ai import skills

        try:
            if name == LOAD_TOOL:
                result = self.load(arguments.get("names") or ([arguments["name"]] if arguments.get("name")
                                                              else []))
            elif name == LIST_SKILLS:
                result = {"skills": [{k: s[k] for k in ("name", "title", "when")}
                                     for s in skills.list_skills() if s["available"]]}
            elif name == READ_SKILL:
                result = {"name": arguments.get("name"), "text": skills.read_skill(str(arguments.get("name")))}
            elif name == READ_ARTIFACT:
                result = offloading.read_artifact(arguments.get("artifact_id", ""), start=arguments.get("start"),
                                                  end=arguments.get("end"), query=arguments.get("query"),
                                                  root=self.offload_root)
            else:
                return error_outcome(tool_use_id, name, f"{name} is handled by the conversation, not here.",
                                     source="local")
        except KeyError as exc:
            return error_outcome(tool_use_id, name, str(exc).strip("'\""), source="local")
        return self.wrap(tool_use_id, name, result, source="local")

    def wrap(self, tool_use_id: str, name: str, result, *, source: str = "local", **fields) -> ToolOutcome:
        """A successful result as a tool_result: bounded text, offloaded when large."""
        from plexora.mcp import serialize

        text = result if isinstance(result, str) else serialize.dumps(result)
        stub = offloading.offload(result, self.offload_tokens, root=self.offload_root) \
            if offloading.tokens(text) > self.offload_tokens else None
        if stub is not None:
            text = canonical(stub)
        elif not isinstance(result, str):
            text = serialize.bound(result)
        return ToolOutcome(tool_use_id, name, [text_block(text)], source=source,
                           offloaded=stub["artifact_id"] if stub else None,
                           summary=_summary(result), result=result, **fields)

    def _registry(self, tool_use_id: str, name: str, arguments: dict, policy) -> ToolOutcome:
        from plexora.agent import registry, revision
        from plexora.mcp import serialize
        from plexora.mcp.tools import split_images

        cap = self.capability(name)

        def call():
            return registry.invoke(self.session, cap.name, arguments, policy=policy, audit=self.audit,
                                   link=self.link, notify=self.notify)

        if self.cache is not None:
            project = arguments.get("project") if isinstance(arguments.get("project"), str) else None
            outcome, source = self.cache.get_or_call(cap, arguments, revision.token(project), call)
        else:
            outcome, source = call(), "live"
        fields = {"capability": cap.name, "permission": cap.permission}
        if not outcome.get("ok"):
            error = outcome.get("error") or {}
            text = serialize.bound({"error": error, "operation_id": outcome.get("operation_id")})
            return ToolOutcome(tool_use_id, name, [text_block(text)], is_error=True, source=source,
                               summary=f"{error.get('code', 'error')}: {error.get('message', '')}"[:300],
                               result={"error": error}, **fields)
        result = outcome.get("result")
        if isinstance(result, dict):
            result = dict(result)                 # a cached answer is never mutated
        result, images = split_images(result)
        receipt = (result or {}).get("receipt") if isinstance(result, dict) else None
        op_id = None
        undo = False
        if cap.permission != "read":
            op_id = (receipt or {}).get("operation_id") or outcome.get("operation_id")
            undo = bool(receipt and receipt.get("reversible", True) and receipt.get("changed", True)
                        and receipt.get("undo_hint"))
        wrapped = self.wrap(tool_use_id, name, result, source=source, operation_id=op_id, undo=undo, **fields)
        if images:
            shown = images[:MAX_RESULT_IMAGES]
            wrapped.content = [image_block(data, fmt) for data, fmt in shown] + wrapped.content
            wrapped.images = shown
        return wrapped


def _summary(result) -> str:
    if isinstance(result, str):
        return result[:200]
    if isinstance(result, dict):
        keys = [k for k in result if not str(k).startswith("_")][:6]
        return "{" + ", ".join(keys) + ("" if len(result) <= 6 else ", ...") + "}"
    if isinstance(result, list):
        return f"[{len(result)} items]"
    return str(result)[:200]

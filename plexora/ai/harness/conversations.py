"""Plexora AI conversations: spatialagent's chat runtime, bound to Plexora.

The conversation itself -- the tool loop, deferred schemas, approvals,
sub-agents, compaction, the tool-result cache, offloading, the store and the
service that runs turns on threads -- is `spatialagent`'s. What this module
adds is Plexora:

- the tools are the capability registry (`backend.RegistryBackend`), driven
  with this process's `AgentSession`, viewer link and notifier;
- the words are Plexora's (`PROFILE`): the identity, house rules and
  `dataset-triage` skill of `prefix.chat_prefix`, the sub-agent brief, the
  disclosure, the compaction note and what `list_skills` and `server_info`
  say they are; the tasks are `chat.turn` and `chat.subagent`, on the wire
  `plexora.chat.turn` and `plexora.chat.subagent`;
- the places on disk are Plexora's: conversations under
  `<data_root>/.agent/ai/conversations/<id>/` (`session.json`,
  `messages.json`, the numbered transcript `decisions.jsonl`, `control.json`,
  `.lock`), offloaded results shared under `.agent/ai/offload/`, the tool
  cache (`toolcache.py`) and the trace (`trace.py`) under `.agent/ai/`;
- `service()` is this process's one `ChatService` -- the server's, or a
  CLI's -- which the `ai.chat_*` capabilities (`chat_capabilities.py`), the
  HTTP blueprint (`server/routes/ai_chat_routes.py`) and `plexora ai chat`
  drive. A write's receipt reaches the open viewer tabs through
  `_notify_viewers`.

`AgentRunner`, `ToolAdapter` and `ChatService` here are spatialagent's with
those defaults, so `AgentRunner.create(store, gateway=..., policy=...)` starts
a Plexora conversation.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from spatialagent import conversations as _conversations
from spatialagent import runner as _runner
from spatialagent import tools as _tools
from spatialagent.runner import DISCLOSURE, ChatProfile
from spatialagent.tools import LIST_SKILLS, SERVER_INFO

from plexora.ai.harness import prefix
from plexora.ai.harness.backend import RegistryBackend
from plexora.ai.harness.toolcache import ToolResultCache

__all__ = ["PROFILE", "AgentRunner", "ChatService", "ConversationStore", "ToolAdapter", "default_root",
           "offload_root", "service", "set_service"]

log = logging.getLogger("plexora.ai.harness")

#: What a Plexora conversation says in Plexora's name, and the tasks it is filed under.
PROFILE = ChatProfile(
    prefix=prefix.chat_prefix,
    subagent_brief=prefix.subagent_brief,
    disclosure=DISCLOSURE,
    turn_task="chat.turn",
    subagent_task="chat.subagent",
    feature="chat",
    summary_heading="[Summary of the earlier conversation, made by Plexora when it grew long]",
    tool_descriptions={
        LIST_SKILLS: "Plexora's runtime skills: name, title and when to use each.",
        SERVER_INFO: "What this Plexora is: version, data directory, the permissions this conversation has, "
                     "which plugins' tools are loaded, the skills and the licence.",
    },
)


def default_root() -> Path:
    from plexora import paths

    return paths.agent_root() / "ai" / "conversations"


def offload_root() -> Path:
    """Offloaded results, shared by every conversation: the same result is
    stored once, under one id, whichever conversation offloaded it."""
    from plexora import paths

    return paths.agent_root() / "ai" / "offload"


_offload_root = offload_root


def _session():
    from plexora.agent.session import AgentSession

    return AgentSession()


def _trace():
    from plexora.ai.harness.trace import TraceStore

    return TraceStore()


class ConversationStore(_conversations.ConversationStore):
    HOLDER = "another Plexora process"
    LIST_HINT = "gating_session_status lists the sessions"

    def __init__(self, root: Path | str | None = None):
        super().__init__(Path(root) if root else default_root())


class ToolAdapter(_tools.ToolAdapter):
    """The registry's tools for one conversation, with Plexora's local-tool
    wording and shared offload root."""

    def __init__(self, session, *, policy, catalog: list | None = None, loaded: list | None = None,
                 cache=None, link=None, notify=None, audit=None, viewer: bool = False,
                 offload_tokens: int = 4000, offload_root=None):
        super().__init__(RegistryBackend(session, link=link, notify=notify, audit=audit), policy=policy,
                         catalog=catalog, loaded=loaded, cache=cache, viewer=viewer,
                         offload_tokens=offload_tokens, offload_root=offload_root or _offload_root(),
                         descriptions=PROFILE.tool_descriptions)

    @property
    def session(self):
        return self.backend.session


class AgentRunner(_runner.AgentRunner):
    """A Plexora conversation's agent: spatialagent's runner over the registry,
    with Plexora's profile, trace, offload root and logger unless told otherwise."""

    @classmethod
    def _defaults(cls, options: dict, session, link, notify) -> dict:
        options["backend"] = options.get("backend") or RegistryBackend(session or _session(), link=link,
                                                                       notify=notify)
        options["trace"] = options.get("trace") or _trace()
        options["profile"] = options.get("profile") or PROFILE
        options["offload_root"] = options.get("offload_root") or _offload_root()
        options["logger"] = options.get("logger") or log
        return options

    @classmethod
    def create(cls, store, *, gateway, policy, session=None, link=None, notify=None,
               **options) -> "AgentRunner":
        return super().create(store, gateway=gateway, policy=policy,
                              **cls._defaults(options, session, link, notify))

    @classmethod
    def open(cls, store, conversation_id: str, *, gateway, session=None, link=None, notify=None,
             **options) -> "AgentRunner":
        return super().open(store, conversation_id, gateway=gateway,
                            **cls._defaults(options, session, link, notify))


class ChatService(_conversations.ChatService):
    """Every Plexora conversation this process drives."""

    def __init__(self, store: ConversationStore | None = None, *, gateway_factory=None, session=None,
                 cache=None, trace=None, link=None, notify=None, runner_options: dict | None = None):
        super().__init__(store or ConversationStore(), gateway_factory=gateway_factory or _default_gateway,
                         backend_factory=self._backend, profile=PROFILE, cache=cache, trace=trace,
                         runner_options=runner_options)
        self._session = session
        self.link = link
        self.notify = notify

    @property
    def session(self):
        if self._session is None:
            self._session = _session()
        return self._session

    def _backend(self) -> RegistryBackend:
        return RegistryBackend(self.session, link=self.link, notify=self.notify)

    def _cache(self):
        if self.cache is None:
            self.cache = ToolResultCache()
        return self.cache

    def _runner_class(self):
        return AgentRunner


def _notify_viewers(project, plugin, kind, body):
    """Tell the tabs open on `project` (plexora.api.notify_viewers): a write's
    receipt reloads their gates, a gating session raises the sidebar's agent
    card. Without it nothing a conversation did reached the viewer -- gates it
    set stayed stale on screen and a session it started ran with no card. In a
    process with no viewer tabs (a CLI) it tells nobody."""
    from plexora import api

    return api.notify_viewers(project, plugin, kind, body)


def _default_gateway():
    from plexora.ai.harness.gateway import GatewayClient

    return GatewayClient()


_SERVICE: ChatService | None = None
_SERVICE_LOCK = threading.Lock()


def service() -> ChatService:
    """This process's chat service (the server's, or a CLI's)."""
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = ChatService(notify=_notify_viewers)
        return _SERVICE


def set_service(value: ChatService | None) -> None:
    """Replace the process's service (tests; a notebook with its own gateway)."""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = value

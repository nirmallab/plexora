"""Judgments reused on identical evidence: the same packet gets the same answer.

Every packet is deterministic -- the same data, partners, code and seed draw
the same numbers and the same image bytes -- so its content is a key. When an
agent answers, the answer is kept under that key (per project, per agent);
when a later session issues a packet with the same key and the session
reuses answers (`SessionOptions.reuse_answers`), the kept answer is applied
at once, and the rerun reaches the same gates without asking again. Answers
are kept per `SessionOptions.agent`, so a different model's judgment is never
mistaken for this one's.

The memo holds judgments, not gates: a changed partner gate, a new profile
version or a different expression matrix changes the evidence, hence the
key, and the question is asked afresh.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

#: Bumped when the key's recipe changes (every earlier key then misses).
VERSION = "1"

#: Packet fields that vary between identical questions (ids, charges, the
#: viewer): left out of the key.
VOLATILE = ("session_id", "packet_id", "budget", "progress", "mirror", "rerendered",
            "answer_schema", "answer_with", "images", "narration")

_LOCK = threading.Lock()


def key(packet, images, *, versions=()) -> str:
    """A content hash of what the agent is shown: the packet's JSON (less
    `VOLATILE`), every image's bytes, and the versions of the code that drew
    them."""
    from plexora.plugins.gating.server.autogate import lattice, schemas

    digest = hashlib.sha256()
    body = {k: v for k, v in packet.items() if k not in VOLATILE and not k.startswith("_")}
    digest.update(json.dumps(body, sort_keys=True, separators=(",", ":"),
                             default=str).encode("utf-8"))
    for data, fmt in images:
        digest.update(str(fmt).encode("utf-8"))
        digest.update(bytes(data))
    for version in (VERSION, schemas.PROFILE_VERSION, lattice.VERSION, *versions):
        digest.update(str(version).encode("utf-8"))
    return digest.hexdigest()


def _safe(name):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(name)) or "_"


def path(project) -> Path:
    from plexora import paths

    return paths.agent_root() / "memo" / "gating" / f"{_safe(project)}.jsonl"


def get(project, packet_key, agent):
    """The answer `agent` gave to this packet before, or None."""
    target = path(project)
    if not target.is_file():
        return None
    found = None
    with _LOCK:
        for line in target.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("key") == packet_key and entry.get("agent") == agent:
                found = entry
    return found


def put(project, packet_key, agent, answer, **about) -> None:
    target = path(project)
    target.parent.mkdir(parents=True, exist_ok=True)
    entry = {"key": packet_key, "agent": agent, "answer": answer, **about}
    with _LOCK, target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True, default=str) + "\n")


def forget(project, *, agent=None) -> int:
    """Drop the kept answers of a project (of one agent); returns how many."""
    target = path(project)
    if not target.is_file():
        return 0
    with _LOCK:
        lines = target.read_text(encoding="utf-8").splitlines()
        kept = [l for l in lines if agent is not None and json.loads(l).get("agent") != agent]
        target.write_text("".join(l + "\n" for l in kept), encoding="utf-8")
    return len(lines) - len(kept)

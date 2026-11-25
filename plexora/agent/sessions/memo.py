"""Judgments reused on identical evidence: the same packet gets the same answer.

Every packet a session issues is deterministic -- the same data, code and seed
draw the same numbers and the same image bytes -- so its content is a key.
When an agent answers, the answer is kept under that key (per workflow, per
project, per agent); when a later session issues a packet with the same key and
reuses answers, the kept answer is applied at once and the rerun reaches the
same decisions without asking again. Answers are kept per agent, so a
different model's judgment is never mistaken for this one's.

The memo holds judgments, not results: changed evidence changes the key, and
the question is asked afresh. A workflow names the code versions that drew its
evidence (`versions`), so a new detector or renderer misses every earlier key.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

#: Bumped when the key's recipe changes (every earlier key then misses).
VERSION = "1"

#: Packet fields that vary between identical questions (ids, charges, the
#: viewer, the reader it went to): left out of the key.
VOLATILE = ("session_id", "packet_id", "budget", "progress", "mirror", "rerendered",
            "answer_schema", "answer_with", "images", "narration", "briefed")

_LOCK = threading.Lock()


def key(packet, images, *, versions=()) -> str:
    """A content hash of what the agent is shown: the packet's JSON (less
    `VOLATILE`), every image's bytes, and `versions`."""
    digest = hashlib.sha256()
    body = {k: v for k, v in packet.items() if k not in VOLATILE and not k.startswith("_")}
    digest.update(json.dumps(body, sort_keys=True, separators=(",", ":"),
                             default=str).encode("utf-8"))
    for data, fmt in images:
        digest.update(str(fmt).encode("utf-8"))
        digest.update(bytes(data))
    for version in (VERSION, *versions):
        digest.update(str(version).encode("utf-8"))
    return digest.hexdigest()


def _safe(name):
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in str(name)) or "_"


def path(kind, project) -> Path:
    from plexora import paths

    return paths.agent_root() / "memo" / _safe(kind) / f"{_safe(project)}.jsonl"


#: str(path) -> ((mtime_ns, size), {(key, agent): entry}): each memo file read
#: once, not once per packet -- a session asks the memo before every packet it
#: issues, and with several packets out it asked as often again. A file whose
#: modification time or size changed (another process appended, or `forget`)
#: is read afresh.
_INDEX: dict = {}


def _stamp(target):
    stat = target.stat()
    return (stat.st_mtime_ns, stat.st_size)


def _index(target) -> dict:
    """{(key, agent): entry} of a memo file, the last entry of a key winning;
    called with `_LOCK` held."""
    stamp = _stamp(target)
    held = _INDEX.get(str(target))
    if held is not None and held[0] == stamp:
        return held[1]
    entries = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        entries[(entry.get("key"), entry.get("agent"))] = entry
    _INDEX[str(target)] = (stamp, entries)
    return entries


def get(kind, project, packet_key, agent):
    """The answer `agent` gave to this packet before, or None."""
    target = path(kind, project)
    if not target.is_file():
        return None
    with _LOCK:
        return _index(target).get((packet_key, agent))


def put(workflow, project, packet_key, agent, answer, /, **about) -> None:
    target = path(workflow, project)
    target.parent.mkdir(parents=True, exist_ok=True)
    entry = {"key": packet_key, "agent": agent, "answer": answer, **about}
    line = json.dumps(entry, sort_keys=True, default=str) + "\n"
    with _LOCK:
        held = _INDEX.get(str(target))
        current = held is not None and target.is_file() and held[0] == _stamp(target)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)
        if current:
            # The index held the whole file: it takes this entry too, read
            # back as written (JSON), as a fresh read of the file would.
            held[1][(packet_key, agent)] = json.loads(line)
            _INDEX[str(target)] = (_stamp(target), held[1])
        else:
            _INDEX.pop(str(target), None)


def forget(kind, project, *, agent=None) -> int:
    """Drop the kept answers of a project (of one agent); returns how many."""
    target = path(kind, project)
    if not target.is_file():
        return 0
    with _LOCK:
        lines = target.read_text(encoding="utf-8").splitlines()
        kept = [l for l in lines if agent is not None and json.loads(l).get("agent") != agent]
        target.write_text("".join(l + "\n" for l in kept), encoding="utf-8")
        _INDEX.pop(str(target), None)
    return len(lines) - len(kept)

"""Per-client bearer tokens for the MCP server's HTTP transport.

stdio needs none: the client launched the server and owns its pipes. Over HTTP
anything that can reach the port can call, so every request carries a token,
and each token has a scope that narrows what the server itself was started to
allow (`Policy.narrowed_by_scope`) -- never widens it:

- `read`: reads only (renders included, as the server's egress allows);
- `write`: also Plexora's own reversible state (gates, regions);
- `admin`: whatever the server allows, source-file writes and deletes included;
- `bridge`: another application on the same dataset (SCIMAP Pro, through
  spatialbridge) -- the `write` policy, and calls made with it carry the
  `bridge` origin, so a Paid capability does not also need the `mcp` add-on.

Kept in `<data_root>/.agent/tokens.json`, owner-readable only, as SHA-256
digests: the secret is shown once, when it is made, and never stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCOPES = ("read", "write", "admin", "bridge")

TOKENS_FILENAME = "tokens.json"

_LOCK = threading.Lock()

#: `last_used` is written at most this often per token (it costs a file write).
LAST_USED_RESOLUTION_S = 60


def default_path() -> Path:
    from plexora import paths

    return paths.agent_root() / TOKENS_FILENAME


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp):
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class TokenStore:
    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path is not None else None
        self._last_touch: dict = {}

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else default_path()

    def _load(self) -> dict:
        import json

        path = self.path
        if not path.exists():
            return {"version": 1, "tokens": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": 1, "tokens": {}}
        data.setdefault("tokens", {})
        return data

    def _save(self, data: dict):
        from plexora.server.models.secret_store import write_private_json

        write_private_json(self.path, data)

    def create(self, scope: str = "read", label: str = "", expires_days: float | None = None):
        """(secret, record). The secret is returned here and nowhere else."""
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}, not {scope!r}")
        token_id = secrets.token_hex(4)
        secret = f"plx_{token_id}_{secrets.token_urlsafe(32)}"
        created = _now()
        record = {
            "id": token_id, "digest": _digest(secret), "scope": scope,
            "label": str(label or ""), "created": _iso(created),
            "expires": _iso(created + timedelta(days=float(expires_days)))
            if expires_days else None,
            "last_used": None, "revoked": False,
        }
        with _LOCK:
            data = self._load()
            data["tokens"][token_id] = record
            self._save(data)
        return secret, self.public(record)

    @staticmethod
    def public(record: dict) -> dict:
        return {key: value for key, value in record.items() if key != "digest"}

    def list(self) -> list:
        records = self._load()["tokens"].values()
        return [self.public(r) for r in sorted(records, key=lambda r: r.get("created") or "")]

    def revoke(self, token_id: str) -> bool:
        with _LOCK:
            data = self._load()
            record = data["tokens"].get(token_id)
            if record is None:
                return False
            record["revoked"] = True
            self._save(data)
        return True

    def live(self, record: dict) -> bool:
        if record.get("revoked"):
            return False
        expires = _parse(record.get("expires"))
        return expires is None or expires > _now()

    def count(self) -> int:
        """Tokens that would be accepted now."""
        return sum(1 for record in self._load()["tokens"].values() if self.live(record))

    def verify(self, secret: str) -> dict | None:
        """The public record of a live token, or None. Constant-time on the digest."""
        if not isinstance(secret, str) or not secret.startswith("plx_"):
            return None
        parts = secret.split("_", 2)
        if len(parts) != 3:
            return None
        record = self._load()["tokens"].get(parts[1])
        if record is None or not hmac.compare_digest(record.get("digest", ""),
                                                     _digest(secret)):
            return None
        if not self.live(record):
            return None
        self._touch(record["id"])
        return self.public(record)

    def _touch(self, token_id: str):
        now = time.monotonic()
        if now - self._last_touch.get(token_id, float("-inf")) < LAST_USED_RESOLUTION_S:
            return
        self._last_touch[token_id] = now
        try:
            with _LOCK:
                data = self._load()
                if token_id in data["tokens"]:
                    data["tokens"][token_id]["last_used"] = _iso(_now())
                    self._save(data)
        except OSError:  # pragma: no cover - a read-only root still verifies
            pass

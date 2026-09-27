"""Bearer tokens for the HTTP transport, as the MCP SDK's auth expects them.

The SDK's bearer middleware calls `verify_token` for every request to the MCP
endpoint and refuses the request (401) when it returns None; what it returns
is then readable, for the rest of that request, through `get_access_token()`
-- a context variable, so it must be read on the event-loop task handling the
request, never on the worker thread a capability runs on. `policy_for` turns
the token into the policy that request runs under.
"""

from __future__ import annotations

from datetime import datetime, timezone


class PlexoraTokenVerifier:
    """`mcp.server.auth.provider.TokenVerifier` over Plexora's token store."""

    def __init__(self, store=None):
        from plexora.agent.tokens import TokenStore

        self.store = store or TokenStore()

    async def verify_token(self, token: str):
        import anyio

        from mcp.server.auth.provider import AccessToken

        record = await anyio.to_thread.run_sync(self.store.verify, token)
        if record is None:
            return None
        expires_at = None
        if record.get("expires"):
            expires_at = int(datetime.strptime(record["expires"], "%Y-%m-%dT%H:%M:%SZ")
                             .replace(tzinfo=timezone.utc).timestamp())
        return AccessToken(token=token, client_id=record["id"], scopes=[record["scope"]],
                           expires_at=expires_at,
                           claims={"label": record.get("label") or ""})


def current_token():
    """The access token of the request being handled, or None (stdio, no auth)."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ImportError:  # pragma: no cover
        return None
    return get_access_token()


def policy_for(base, token):
    """The policy a request runs under: the server's, narrowed by its token."""
    if token is None:
        return base
    scope = token.scopes[0] if token.scopes else "read"
    return base.narrowed_by_scope(scope, principal=f"token:{token.client_id}")


def auth_settings():
    """What the SDK needs to enable its bearer middleware, and no more: no
    OAuth server, no protected-resource metadata routes."""
    from mcp.server.auth.settings import AuthSettings

    return AuthSettings(issuer_url="http://127.0.0.1", resource_server_url=None)

"""The website's licence: held by the page, read here.

On the website the browser is the product process (plan D8). The page
(client services/licenseClient.js) runs the device flow against the BioCognia
platform, verifies the certificate with the platform's own verifier, keeps it
in the site's IndexedDB with a random device secret, and renews it. Python
never talks to the platform there. It reads what the page writes into
BIOCOGNIA_DIR (/bioc in the worker, plexora/browser/boot.py) -- `license.json`
and `environment.json`, the files the `biocognia` package itself writes on a
desktop -- so `licensing.peek()`, Jinja's `licensed()` and the capability
guards run unchanged, and offline.

- `adopt(files)`: the page's records written as the package's own files (or
  removed), the licence resolved again from them, and the status every
  licence surface shows returned (`license_routes.status_payload`). The
  worker calls it (`licence` op) at boot and after every change.
- `PageTokenSource`: the 30-minute AI token asked of the page, which
  exchanges it against the certificate and holds it in memory, instead of
  the platform. `plexora.ai.harness.gateway.TokenSource` takes it on the
  website (`token_source`).
- A test build of the site (`static_build --licence-test-key`) also trusts a
  throwaway key: `bct*` kids only, never one the package already trusts.
"""

from __future__ import annotations

import json
import os
import re
import time

#: What license.json and environment.json may carry from the page: exactly
#: what biocognia/state.py `save_certificates` and environment.py write.
LICENSE_FIELDS = ("certificate", "source", "credential_hint", "installed_at", "last_validated",
                  "last_attempt", "hwm", "server", "environment", "revoked")
ENVIRONMENT_FIELDS = ("secret", "created_at")

#: biocognia.testing's throwaway kids (`bct`, `bct2`, ...).
_TEST_KID = re.compile(r"^bct\d*$")
_PUBLIC_KEY = re.compile(r"^[A-Za-z0-9_-]{43}$")

_asker = None


def on_the_website() -> bool:
    return os.environ.get("PLEXORA_BACKEND") == "browser"


def set_asker(ask) -> None:
    """`ask(kind, args)` -> the page's answer, or None. Installed by the
    website's worker at boot (boot.start); None removes it."""
    global _asker
    _asker = ask


def _plain(value):
    """A page's answer as Python values (a JS object arrives as a proxy)."""
    to_py = getattr(value, "to_py", None)
    return to_py() if callable(to_py) else value


def test_keys(keys) -> dict:
    """The throwaway keys a test build trusts: `bct*` kids with a well-formed
    key, never a kid the package already trusts (a production key cannot be
    replaced this way)."""
    from biocognia import keys as key_table

    out = {}
    for kid, value in (keys or {}).items():
        if isinstance(kid, str) and _TEST_KID.match(kid) and isinstance(value, str) \
                and _PUBLIC_KEY.match(value) and kid not in key_table.PUBLIC_KEYS:
            out[kid] = value
    return out


def adopt(files) -> dict:
    """Write the page's licence records where `biocognia` reads them, resolve
    the licence again, and return the status (no network: on the website the
    heartbeat is the page's, BIOCOGNIA_NO_HEARTBEAT).

    `files`: `{license: record | None, environment: record | None, keys: {kid:
    key} | None}`. A record that is absent removes its file, so Forget leaves
    nothing behind."""
    from biocognia import keys as key_table
    from biocognia import store

    from plexora.licensing import LICENSING

    files = files if isinstance(files, dict) else {}
    extra = test_keys(files.get("keys"))
    if extra:
        key_table.PUBLIC_KEYS = {**key_table.PUBLIC_KEYS, **extra}

    environment = files.get("environment")
    if isinstance(environment, dict) and isinstance(environment.get("secret"), str) \
            and len(environment["secret"]) >= 32:
        store.write_environment({k: environment[k] for k in ENVIRONMENT_FIELDS if k in environment})
    else:
        store.clear_environment()

    record = files.get("license")
    if isinstance(record, dict) and isinstance(record.get("certificate"), str) and record["certificate"]:
        LICENSING.store.write({k: record[k] for k in LICENSE_FIELDS if k in record})
    else:
        LICENSING.store.clear()

    LICENSING.reset()
    from plexora.server.routes.license_routes import status_payload

    return status_payload()


def adopt_json(text: str) -> str:
    """`adopt` for the worker: JSON in, JSON out (no proxies cross)."""
    return json.dumps(adopt(json.loads(text or "{}")))


def token_source(licensing, token: str | None = None):
    """The page's token source on the website, or None (anywhere else, or when
    a token was given outright: `BIOCOGNIA_AI_TOKEN`)."""
    from biocognia.gateway import ENV_TOKEN

    if token or (os.environ.get(ENV_TOKEN) or "").strip() or _asker is None or not on_the_website():
        return None
    return _token_class()(licensing)


_TOKEN_CLASS = None


def _token_class():
    global _TOKEN_CLASS
    if _TOKEN_CLASS is not None:
        return _TOKEN_CLASS
    from biocognia import gateway

    class PageTokenSource(gateway.TokenSource):
        """biocognia's TokenSource, with the token asked of the page: on the
        website the page holds the certificate and exchanges it
        (`/v1/ai/token`), and Python keeps the token in memory as a desktop's
        TokenSource does, asking again at two thirds of its life or when the
        gateway says it has expired (`force`)."""

        def get(self, *, force: bool = False) -> str:
            if self._fixed:
                return self._fixed
            with self._lock:
                if force or not self._token or time.time() >= self._refresh_at:
                    self._obtain(force=force)
                return self._token  # type: ignore[return-value]

        def _obtain(self, force: bool = False) -> None:
            asker = _asker
            answer = _plain(asker("ai_token", {"force": bool(force)})) if asker is not None else None
            if not isinstance(answer, dict):
                raise gateway.GatewayError("AI needs this browser's licence, and the page did not answer.",
                                           code="no_license")
            error = answer.get("error")
            if isinstance(error, dict):
                retry = error.get("retry_after")
                raise gateway.GatewayError(str(error.get("message") or "AI sign-in failed."),
                                           code=str(error.get("code") or "unreachable"),
                                           status=error.get("status") if isinstance(error.get("status"), int) else None,
                                           retry_after=float(retry) if isinstance(retry, (int, float)) else None)
            if not isinstance(answer.get("token"), str):
                raise gateway.GatewayError("AI sign-in: the page answered without a token.",
                                           code="bad_response")
            now = time.time()
            server_time = float(answer.get("server_time") or now)
            life = max(60.0, float(answer.get("expires_at") or server_time + 1800) - server_time)
            self._token = answer["token"]
            self._refresh_at = now + life * 2 / 3
            self.gateway = answer.get("gateway") or self.gateway
            self.mode = answer.get("mode")
            self.mods = list(answer.get("mods") or [])
            self.balance = dict(answer.get("balance") or {})

    _TOKEN_CLASS = PageTokenSource
    return PageTokenSource


def __getattr__(name):
    # `PageTokenSource` is built on first use: importing this module loads
    # nothing of biocognia's gateway (boot imports it to install the asker).
    if name == "PageTokenSource":
        return _token_class()
    raise AttributeError(name)

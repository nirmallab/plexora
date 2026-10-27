"""What a request must look like before the viewer acts on it.

The viewer has no accounts: reaching its port is the permission, and loopback
is what keeps that port to the person at this machine. Two things a web page in
that person's browser can still do get past loopback, and this module is the
answer to both.

* **A cross-site write.** Any page can POST a form, or a `text/plain` body, to
  `http://127.0.0.1:8765/...`; the browser sends it without a preflight and the
  page simply never sees the reply. It does not need to see it -- the request is
  the attack. Browsers mark every request with where it came from
  (`Sec-Fetch-Site`, and `Origin` on anything that is not a GET), so a write
  that says "cross-site" or "same-site" is refused. "same-site" is refused too
  because the port is not part of a site: another server on this machine --
  a dev server, a notebook, anything on `localhost:<other port>` -- is
  same-site with the viewer and is exactly as much a stranger to it.

* **DNS rebinding.** A page on `evil.example` re-points its own name at
  127.0.0.1 and then talks to the viewer as same-origin, reading replies too.
  The one thing it cannot change is the `Host` header, which still says
  `evil.example`. A viewer bound to loopback therefore serves only the names
  loopback actually has.

What is deliberately NOT refused: a request carrying none of these headers.
That is every non-browser client -- the MCP server, the askpass helper, `curl`,
the notebook's readiness probe, the Flask test client -- and none of them can be
steered by a web page. A browser always sends them, so their absence is not a
loophole a page can use.

Stdlib only and imports nothing from `plexora`, so `plexora/__init__.py` can
use it while it is still initialising (the same rule as `plexora._url`).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

#: The header a non-browser client may carry the server token in, instead of
#: the `?token=` query or the cookie a browser holds.
TOKEN_HEADER = "X-Plexora-Token"

#: Historical cookie name, still accepted when read: a page opened before the
#: per-port name existed keeps working until its next entry URL.
LEGACY_COOKIE = "plexora_auth"

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_TRUSTED_FETCH_SITES = frozenset({"same-origin", "none"})


def host_name(host_header) -> str:
    """The name part of a `Host` header, lower-cased, without port or brackets."""
    host = str(host_header or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else host[1:]
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def host_port(host_header) -> str:
    """The port part of a `Host` header, or "" when it has none."""
    host = str(host_header or "").strip()
    if host.startswith("["):
        rest = host[host.find("]") + 1:] if "]" in host else ""
        return rest[1:] if rest.startswith(":") else ""
    if host.count(":") == 1:
        port = host.split(":", 1)[1]
        return port if port.isdigit() else ""
    return ""


def is_always_trusted(name) -> bool:
    """IP literals, `localhost` and `*.localhost`: names no page can rebind.

    An IP literal is not a name at all, so there is nothing to re-point; and
    browsers resolve `localhost` and its subdomains to loopback themselves,
    without asking DNS.
    """
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def trusted_host(host_header, extra_hosts=()) -> bool:
    """Whether a loopback-bound viewer should answer this `Host` header."""
    name = host_name(host_header)
    if is_always_trusted(name):
        return True
    return name in {host_name(h) for h in extra_hosts or () if h}


def _origin_host(origin) -> str:
    try:
        parts = urlsplit(origin)
    except ValueError:
        return ""
    return (parts.netloc or "").lower()


def cross_site_refusal(method, headers, *, acting=False,
                       allowed_origins=()) -> str | None:
    """Why this request must not be acted on, or None when it may be.

    `headers` is anything with `.get()` -- Werkzeug's `Headers` or a dict.
    Only writes are judged, plus a GET the caller marks `acting` (the few
    routes that change something on a GET); see the module docstring for
    what passes and why.
    """
    if not acting and str(method or "").upper() in _SAFE_METHODS:
        return None
    fetch_site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    origin = (headers.get("Origin") or "").strip()
    if not fetch_site and not origin:
        return None
    if fetch_site in _TRUSTED_FETCH_SITES:
        return None
    if fetch_site and fetch_site not in ("same-site", "cross-site"):
        # A value a future browser invents: judged by Origin below, as an
        # older browser that sends no Sec-Fetch-Site at all is.
        fetch_site = ""
    if fetch_site in ("same-site", "cross-site"):
        if origin and _origin_matches(origin, headers, allowed_origins):
            # A reverse proxy in front of the viewer can make the browser's
            # own idea of "site" disagree with what reached us; an explicit
            # allow-list entry, or an Origin naming the Host it arrived on,
            # settles it.
            return None
        return f"a {fetch_site} request"
    if origin.lower() == "null":
        return "a request from an opaque origin"
    if _origin_matches(origin, headers, allowed_origins):
        return None
    return f"a request from {origin}"


def _origin_matches(origin, headers, allowed_origins) -> bool:
    if not origin or origin.lower() == "null":
        return False
    wanted = origin.rstrip("/").lower()
    if wanted in {str(o).rstrip("/").lower() for o in allowed_origins or () if o}:
        return True
    netloc = _origin_host(origin)
    if not netloc:
        return False
    for header in ("Host", "X-Forwarded-Host"):
        value = (headers.get(header) or "").split(",")[0].strip().lower()
        if value and value == netloc:
            return True
    return False


def auth_cookie_name(host_header) -> str:
    """The cookie this viewer keeps its token in, one name per port.

    Cookies are scoped by host, never by port: every server on 127.0.0.1 --
    two viewers, a viewer and a tunnel to a remote one -- shares one cookie
    jar. Under a single name the second server's token overwrote the first
    one's, and the first viewer's next request was refused.
    """
    port = host_port(host_header)
    return f"{LEGACY_COOKIE}_{port}" if port else LEGACY_COOKIE

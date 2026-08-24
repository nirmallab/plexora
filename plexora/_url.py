"""Base-URL arithmetic, in one place.

Three modules used to carry their own `_clean_base_url`, and two of them meant
different things by it: `plexora/__init__.py` and `plexora/cli.py` returned a
prefix with NO trailing slash (`/user/me`), while `plexora/jupyter.py` returned
one WITH (`/user/me/`) because it immediately concatenated `proxy/<port>` onto
the end. Same name, same-looking body, opposite contracts -- which is exactly
the shape of bug that survives review. The two forms both have a legitimate
caller, so they are kept, but under names that say which is which.

The third function here is the reason this module exists at all.
`clean_prefix` REFUSES a full origin. Everywhere Plexora used the old helper it
was handling a mount path -- "/proxy/8000" -- and prefixing it with "/" is
correct for that. Hosted notebooks introduce a base that is a whole origin
instead: Colab's `google.colab.kernel.proxyPort()` hands back
`https://xxxx.googleusercontent.com`. Feeding that to the old helper produced
`/https:/xxxx.googleusercontent.com` -- a silently corrupt path that fails far
from where it was made. Raising here turns that class of mistake into a message
naming the value.

Deliberately a leaf module: stdlib only, imports nothing from `plexora`, so
`plexora/__init__.py` can use it while it is still initialising.
"""

from __future__ import annotations

from urllib.parse import quote


def is_full_origin(value) -> bool:
    """Whether `value` is an absolute http(s) URL rather than a mount path."""
    if not value:
        return False
    return str(value).strip().lower().startswith(("http://", "https://"))


def clean_prefix(base_url) -> str:
    """A mount path with a leading slash and no trailing one; "" for none.

    This is what `PLEXORA_BASE_URL` and `app.config['PLEXORA_BASE_URL']` hold,
    and what templates concatenate request paths onto. "" and "/" both mean
    "mounted at the root", and both must produce "" rather than "/" so that
    `f"{prefix}/tile/..."` does not come out with a doubled slash.
    """
    if not base_url:
        return ""
    base_url = str(base_url).strip()
    if is_full_origin(base_url):
        raise ValueError(
            f"Base URL must be a mount path such as '/proxy/8000', not a full "
            f"URL: {base_url!r}. A full origin belongs in the DISPLAY url (see "
            f"plexora._url.join_display), never in PLEXORA_BASE_URL -- the "
            f"server mounts routes under a path, and prefixing a scheme with "
            f"'/' would corrupt every link on the page."
        )
    if base_url == "/":
        return ""
    return "/" + base_url.strip("/")


def prefix_with_slash(base_url) -> str:
    """A mount path with slashes on BOTH ends; "/" for none.

    The form callers want when they are about to append a further path segment
    by string concatenation, as the notebook sidecar does with
    `f"{prefix}proxy/{port}"`. Kept separate from `clean_prefix` rather than
    making one of them call `rstrip` at each call site, because the two answers
    for "no prefix" genuinely differ: "" and "/".
    """
    if not base_url or str(base_url).strip() == "/":
        return "/"
    if is_full_origin(base_url):
        raise ValueError(
            f"Base URL must be a mount path, not a full URL: {base_url!r}."
        )
    return "/" + str(base_url).strip().strip("/") + "/"


def join_display(base, *segments) -> str:
    """Build a URL a human or an <iframe> can follow.

    Unlike the two above, `base` here may legitimately be any of three things,
    because a hosted notebook picks a different one in each case:

    - "" -- nothing known; the result is a site-root-relative path.
    - a mount path (`/user/me/proxy/8123`) -- the JupyterHub and Open OnDemand
      case. Stays path-only ON PURPOSE: an iframe resolves it against the
      notebook page's own origin, which is the only origin that carries the
      user's auth cookie, and hardcoding a host would break the moment the hub
      moved or the user came in through a different name.
    - a full origin (`https://xxxx.googleusercontent.com`) -- the Colab case,
      where the proxy is a whole separate subdomain.

    Segments are percent-quoted, so a project named "Tonsil 2 (repeat)" gives a
    URL that survives being pasted into a browser.
    """
    base = "" if base is None else str(base).rstrip("/")
    tail = "/".join(
        quote(str(segment).strip("/"), safe="")
        for segment in segments
        if segment not in (None, "")
    )
    if not tail:
        return f"{base}/" if base else "/"
    return f"{base}/{tail}"

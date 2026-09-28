"""What an entitlement string is, and when one grant covers a requirement.

An entitlement is a colon path -- `ai`, `ai:gating`, `ai:gating:session` --
and a grant covers everything beneath it. A licence that carries `ai` satisfies
a capability that requires `ai:gating:session`; one that carries only
`ai:evidence` does not. This is the whole of the matching rule, and it lives
here once so that no caller ever compares a plan name instead.

`free` is not an entitlement anybody is granted. It is what a capability inside
an entitled plugin says to opt back out of its plugin's requirement, and a
requirement of `free` (or of nothing) is always met.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: One segment is a lowercase identifier; a path is segments joined by colons.
GRAMMAR = re.compile(r"[a-z][a-z0-9_]*(?::[a-z][a-z0-9_]*)*")

#: The explicit opt-out a capability uses inside an entitled plugin.
EXPLICIT_FREE = "free"

#: Longer than any real path by a wide margin; a guard against a certificate
#: stuffed with a megabyte string, not a design limit.
MAX_LENGTH = 128


def valid(entitlement) -> bool:
    """Whether `entitlement` is a well-formed entitlement string."""
    return (isinstance(entitlement, str) and len(entitlement) <= MAX_LENGTH
            and GRAMMAR.fullmatch(entitlement) is not None)


def is_free(required) -> bool:
    """Whether a requirement asks for nothing."""
    return required is None or required == EXPLICIT_FREE


def satisfies(grant: str, required: str) -> bool:
    """Whether one grant covers one requirement: equal, or an ancestor of it."""
    return grant == required or required.startswith(grant + ":")


def any_satisfies(grants: Iterable[str], required) -> bool:
    """Whether any of `grants` covers `required`. Free requirements always pass."""
    if is_free(required):
        return True
    return any(satisfies(grant, required) for grant in grants)


def normalize(grants) -> tuple[str, ...]:
    """A certificate's grant list, as the sorted, de-duplicated valid strings.

    Anything malformed is dropped rather than failing the whole certificate:
    the signature already says the server meant it, and a future server that
    grants something this client cannot parse should cost that one grant, not
    the licence. `free` is dropped too -- it is never a grant.
    """
    if not isinstance(grants, (list, tuple)):
        return ()
    return tuple(sorted({g for g in grants if valid(g) and g != EXPLICIT_FREE}))


def parent(entitlement: str) -> str | None:
    """The next path up, or None for a root."""
    head, _, _ = entitlement.rpartition(":")
    return head or None


def ancestors(entitlement: str) -> tuple[str, ...]:
    """Every path above `entitlement`, nearest first."""
    out = []
    current = parent(entitlement)
    while current is not None:
        out.append(current)
        current = parent(current)
    return tuple(out)

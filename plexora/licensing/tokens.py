"""Entitlement proofs for data nodes.

A data node (`plexora node serve`) runs where the data is -- an HPC compute
node, a lab workstation -- and has no Settings page, no licence and no reason
to contact the licence service; it never should. Most of what it runs is Free
plumbing (tiles, rows, columns). A few table operations exist only to serve
Paid capabilities: automatic gating's evidence and statistics. For those, the
primary attaches a short-lived proof that ITS licence admitted the work:

    X-Plexora-Entitlement-Proof: v1.<entitlements>.<expires>.<hmac>

HMAC-SHA256 under the node token the two already share, so no licence secret
and no certificate ever travels to a node, and nothing is stored there. The
proof lasts an hour, and is minted only when the primary's licence allows the
work -- or, for a job, when it allowed it at submit (see `admitted`), so a job
that started on a valid licence is never cut off halfway.

What this is and is not. The enforcement point is the primary's capability
registry. This is the second check behind it: a Paid code path inside Plexora
that ever reached a node without going through the registry would be refused
here. It is not a barrier against somebody who already holds the node token --
they are, by construction, the primary.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import threading
import time

from plexora.licensing.entitlements import normalize, satisfies, valid

PROOF_HEADER = "X-Plexora-Entitlement-Proof"
PROOF_TTL_SECONDS = 3600
#: A proof dated this far in the future is refused: a clock that is off by an
#: hour between two of the user's own machines is already worth saying.
MAX_SKEW_SECONDS = 300

#: Table operations a node runs only for a licensed primary: operation-name
#: prefix -> the entitlement family they serve.
ENTITLED_OPERATIONS = {
    "gating.autogate.": "ai:gating",
}

_local = threading.local()
_cache: dict = {}
_cache_lock = threading.Lock()


def required_for(operation: str) -> str | None:
    """The entitlement a node operation needs, or None for Free plumbing."""
    for prefix, entitlement in ENTITLED_OPERATIONS.items():
        if operation.startswith(prefix):
            return entitlement
    return None


def _mac(key: str, entitlements: str, expires: int) -> str:
    return hmac.new(key.encode("utf-8"), f"v1|{entitlements}|{expires}".encode("utf-8"),
                    hashlib.sha256).hexdigest()


def mint_proof(node_token: str, entitlements, *, now: float | None = None,
               ttl: int = PROOF_TTL_SECONDS) -> str:
    grants = ",".join(normalize(list(entitlements)))
    expires = int((time.time() if now is None else now) + ttl)
    return f"v1.{grants}.{expires}.{_mac(node_token, grants, expires)}"


def _overlaps(grant: str, required: str) -> bool:
    """A grant anywhere in the required family: its ancestor, itself, or a
    branch of it. Node operations are shared plumbing for the whole family --
    `ai:gating:analytics` and `ai:gating:session` both need the evidence."""
    return satisfies(grant, required) or satisfies(required, grant)


def verify_proof(node_token: str, proof: str | None, required: str, *,
                 now: float | None = None) -> bool:
    if not node_token or not proof or not isinstance(proof, str):
        return False
    parts = proof.strip().split(".")
    if len(parts) != 4 or parts[0] != "v1":
        return False
    _, grants_text, expires_text, mac = parts
    try:
        expires = int(expires_text)
    except ValueError:
        return False
    moment = time.time() if now is None else now
    if expires < moment or expires > moment + PROOF_TTL_SECONDS + MAX_SKEW_SECONDS:
        return False
    if not hmac.compare_digest(_mac(node_token, grants_text, expires), mac):
        return False
    grants = [g for g in grants_text.split(",") if valid(g)]
    return any(_overlaps(grant, required) for grant in grants)


@contextlib.contextmanager
def admitted(grants):
    """Run a block (a job) with the grants its licence had when it was
    admitted, so its node operations keep their proof if the licence lapses
    while it runs."""
    previous = getattr(_local, "grants", None)
    _local.grants = tuple(grants) if grants else None
    try:
        yield
    finally:
        _local.grants = previous


def _current_grants() -> tuple:
    held = getattr(_local, "grants", None)
    if held:
        return held
    from plexora.licensing import state

    current = state.current()
    return current.entitlements if current.paid else ()


def proof_for(node_token: str) -> str | None:
    """The proof to attach for `node_token`, or None when nothing is licensed.
    Cached until a minute before it runs out."""
    if not node_token:
        return None
    grants = _current_grants()
    if not grants:
        return None
    key = (hashlib.sha256(node_token.encode("utf-8")).hexdigest(), tuple(grants))
    now = time.time()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[1] - 60 > now:
            return cached[0]
    proof = mint_proof(node_token, grants, now=now)
    with _cache_lock:
        _cache[key] = (proof, int(now) + PROOF_TTL_SECONDS)
    return proof


def headers_for(node_token: str, operation: str) -> dict:
    """The extra request headers for one node operation: a proof when it is an
    entitled one and something is licensed, nothing otherwise."""
    if required_for(operation) is None:
        return {}
    proof = proof_for(node_token)
    return {PROOF_HEADER: proof} if proof else {}


def reset_for_tests() -> None:
    with _cache_lock:
        _cache.clear()
    _local.grants = None

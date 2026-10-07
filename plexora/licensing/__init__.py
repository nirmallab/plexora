"""Plexora's two plans, Free and Paid, and the one place that tells them apart.

Free is everything Plexora does without a licence: every image, table and
modality, every plugin's manual tools, remote and HPC viewing, notebooks,
export. It needs no account, no network and no activation, and nothing in this
package runs on a Free path -- a capability with no entitlement returns before
the licence is even looked at (`guards.check_capability`).

Paid unlocks entitlements (`manifest.py`), which attach to capabilities and
actions, never to plan names:

    from plexora import licensing
    licensing.allows("ai:gating:session")      # True on a Paid licence

The licence itself is the shared BioCognia client (`biocognia`): a `BIOC1`
certificate the platform signs for `aud: "plexora"`, verified offline against
public keys shipped in that package, resolved lazily and failing open to Free.
A person connects a device at account.biocognia.com; Plexora holds no account.
This module holds Plexora's three handles on it -- `PRODUCT` (the manifest),
`LICENSING` (the state) and `GUARD` (the gate) -- and Plexora's own words for
what the library calls a licensed state: Paid.

Importing this package reads no file, starts no thread and loads no crypto.
"""

from __future__ import annotations

from biocognia import EntitlementRequired, Guard, LicenseError, LicenseState, Licensing, Product
from biocognia.entitlements import is_free

from plexora import __version__
from plexora.licensing import manifest

PRODUCT = Product.from_manifest(manifest.product_manifest(), version=__version__)
LICENSING = Licensing(PRODUCT)
GUARD = Guard(LICENSING)

#: The library's state names, in Plexora's words where they differ. Everything
#: a person or an agent sees -- Settings, `plexora license status`,
#: `server_info` -- says `paid_active` for a licence that is simply working.
STATE_NAMES = {"active": "paid_active"}


def plan_of(state: LicenseState) -> str:
    """`paid` or `free`: whether Paid features run in `state`. Plexora's plan
    is this, never the catalogue's plan id (which `describe()` carries as
    `plan_id`)."""
    return "paid" if state.licensed else "free"


def state_name(state: LicenseState) -> str:
    return STATE_NAMES.get(state.state, state.state)


def current() -> LicenseState:
    """The licence state, resolving it (and exchanging a token) on first use."""
    return LICENSING.current()


def peek() -> LicenseState:
    """The licence state without any network call."""
    return LICENSING.peek()


def allows(entitlement) -> bool:
    """Whether `entitlement` is unlocked. `None`/`free` never touch the licence."""
    if is_free(entitlement):
        return True
    return GUARD.allows(entitlement)


def now() -> float:
    """The licence clock: wall time, never earlier than what this machine has seen."""
    return LICENSING.now()


def describe(*, network: bool = False, state: LicenseState | None = None) -> dict:
    """The public description of the licence, in Plexora's words: `plan`
    (free/paid), `paid`, `state` (`paid_active` for a working licence), grants
    and dates. Never the certificate."""
    resolved = state if state is not None else LICENSING.current(network=network)
    info = resolved.describe(now=LICENSING.now())
    info["plan_id"] = info.get("plan")
    info.update(plan=plan_of(resolved), paid=resolved.licensed, state=state_name(resolved))
    return info


def reload() -> LicenseState:
    return LICENSING.reload()


def reset_for_tests() -> None:
    LICENSING.reset()


__all__ = ["GUARD", "LICENSING", "PRODUCT", "EntitlementRequired", "LicenseError", "LicenseState",
           "allows", "current", "describe", "now", "peek", "plan_of", "reload", "reset_for_tests",
           "state_name"]

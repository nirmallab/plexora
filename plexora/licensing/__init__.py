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

A licence is an Ed25519-signed certificate (`certificate.py`) verified against
public keys shipped here (`keys.py`); the private keys never leave the licence
service. Resolution is lazy and fails open to Free (`state.py`). The only
module that opens a socket is `client.py`.

Importing this package reads no file, starts no thread and loads no crypto.
"""

from __future__ import annotations

from plexora.licensing import state as _state
from plexora.licensing.entitlements import is_free
from plexora.licensing.models import LicenseState


def current() -> LicenseState:
    """The licence state, resolving it (and exchanging a token) on first use."""
    return _state.current()


def peek() -> LicenseState:
    """The licence state without any network call."""
    return _state.peek()


def allows(entitlement) -> bool:
    """Whether `entitlement` is unlocked. `None`/`free` never touch the licence."""
    if is_free(entitlement):
        return True
    return _state.current().allows(entitlement)


def describe(*, network: bool = False) -> dict:
    """The public description of the licence: plan, state, grants, dates."""
    resolved = _state.current(network=network)
    return resolved.describe(now=_state.now())


def reload() -> LicenseState:
    return _state.reload()


def reset_for_tests() -> None:
    _state.reset()


__all__ = ["LicenseState", "allows", "current", "describe", "peek", "reload",
           "reset_for_tests"]

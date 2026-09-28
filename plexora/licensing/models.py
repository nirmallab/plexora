"""The licence state, as one immutable value.

Eight states, and exactly one question each of them answers the same way
everywhere: do Paid features run? Free features run in all eight -- that is
not a rule enforced here, it is the absence of any check on a Free path.

    state          Free  Paid   how you get there
    free            yes   no    no licence at all -- the default, forever
    trial           yes   yes   a valid trial certificate
    paid_active     yes   yes   a valid certificate from an online activation
    offline_valid   yes   yes   a valid certificate from an offline grant
    grace           yes   yes   past expiry, inside the certificate's grace
    expired         yes   no    past expiry and grace
    revoked         yes   no    the licence server said so
    invalid         yes   no    a certificate that does not verify here

`use_class` (academic, commercial, ...) rides along for display. It never
changes what a plan or an entitlement means.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from plexora.licensing.entitlements import any_satisfies

STATES = ("free", "trial", "paid_active", "offline_valid", "grace", "expired",
          "revoked", "invalid")

#: The states in which Paid features run. Everything else is Free.
PAID_STATES = frozenset({"trial", "paid_active", "offline_valid", "grace"})

#: How close to its end an unrenewed online certificate must be before the
#: person is told to connect. Shorter than the 21 days at which the client
#: starts renewing, so anyone online is renewed before they would see it.
RENEW_NOTICE_SECONDS = 14 * 86400

PLANS = ("free", "paid")
USE_CLASSES = ("academic", "commercial", "nonprofit", "government")
ENVIRONMENT_TYPES = ("desktop", "cluster", "container-host", "job")

#: Why a state is what it is, as short codes a UI or an agent can branch on.
REASONS = (
    "no_license",            # nothing installed, nothing in the environment
    "valid",
    "in_grace",
    "expired",
    "revoked",
    "malformed",             # not a certificate at all
    "unknown_key",           # signed with a key this build does not trust
    "bad_signature",
    "wrong_product",         # a certificate for some other product
    "unsupported_version",
    "future_dated",
    "environment_mismatch",  # issued for a different registered environment
    "unknown_plan",
    "unreadable_file",
    "token_not_exchanged",   # a licence token that has not reached the server yet
    "offline_refused",
)


@dataclass(frozen=True)
class LicenseState:
    """Everything the rest of Plexora may know about the licence.

    `plan` and `entitlements` are EFFECTIVE: `paid` and the certificate's grants
    only while the state is one of PAID_STATES, `free` and nothing otherwise.
    A caller that reads them can never grant access by mistake on an expired
    certificate, because an expired certificate has none.
    """

    state: str = "free"
    reason: str = "no_license"
    plan: str = "free"
    entitlements: tuple[str, ...] = ()
    source: str = "none"
    trial: bool = False
    use_class: str | None = None
    license_id: str | None = None
    account_id: str | None = None
    seat_id: str | None = None
    cert_id: str | None = None
    environment_id: str | None = None
    environment_type: str | None = None
    environment_name: str | None = None
    issued_at: float | None = None
    #: When the CERTIFICATE stops -- at most 90 days out, renewed quietly
    #: online. What gates Paid; not what a person is shown.
    expires_at: float | None = None
    #: When the LICENCE ends -- the date a person is shown. None on
    #: certificates issued before it was added.
    license_expires_at: float | None = None
    grace_until: float | None = None
    offline_until: float | None = None
    last_validated: float | None = None
    clock_rollback: bool = False
    #: The certificate string itself. Never shown, never serialised by
    #: `describe`; kept so the heartbeat can present it to the server.
    certificate: str | None = field(default=None, repr=False, compare=False)
    #: The grants the certificate carries whatever the state -- what an
    #: expired licence USED to unlock, for "renew to get X back" messages.
    granted: tuple[str, ...] = ()

    @property
    def paid(self) -> bool:
        return self.state in PAID_STATES

    def allows(self, required) -> bool:
        """Whether this state unlocks `required` (None/`free` always pass)."""
        if required is None or required == "free":
            return True
        return self.paid and any_satisfies(self.entitlements, required)

    def with_state(self, state: str, reason: str) -> LicenseState:
        """This state moved to `state`, with plan and grants made to agree."""
        paid = state in PAID_STATES
        return replace(self, state=state, reason=reason,
                       plan="paid" if paid else "free",
                       entitlements=self.granted if paid else ())

    def days_left(self, now: float) -> int | None:
        """Whole days until Paid features stop, rounded up; None when unbounded."""
        if not self.paid:
            return None
        end = self.grace_until if self.state == "grace" else self.expires_at
        if end is None:
            return None
        return max(0, math.ceil((end - now) / 86400))

    def validity(self, now: float) -> dict:
        """The dates a person needs, and nothing they do not.

        - `until`: when the licence ends. A certificate ending sooner is renewed
          online without anyone noticing, so its date is not this one.
        - `ended`: `until` has passed.
        - `renew` (`online` or `file`) / `renew_by`: set only when the person
          has to act before the licence ends -- an offline licence file that
          runs out sooner, or a certificate near its end that has not been
          renewed because this machine has been offline. `renew` with no
          `renew_by` means Paid is already paused until they do.
        """
        out = {"until": None, "ended": False, "renew_by": None, "renew": None}
        if self.state not in PAID_STATES and self.state != "expired":
            return out  # free, revoked, invalid: no date means anything
        end = self.expires_at
        licence_end = self.license_expires_at
        # A job's licence ends with the job's certificate; an older
        # certificate says nothing more than its own end.
        until = end if self.environment_type == "job" or licence_end is None else licence_end
        out.update(until=until, ended=until is not None and now > until)
        if until is None or out["ended"] or end is None or end >= until:
            return out
        how = "file" if self.offline_until is not None else "online"
        if self.state == "grace":
            out.update(renew_by=self.grace_until, renew=how)
        elif self.state == "expired":
            out.update(renew=how)
        elif how == "file" or end - now < RENEW_NOTICE_SECONDS:
            out.update(renew_by=end, renew=how)
        return out

    def describe(self, now: float | None = None) -> dict:
        """The public shape: what Settings, `plexora license status` and an
        agent may see. No certificate, no secret, no account identifier."""
        out = {
            "plan": self.plan,
            "state": self.state,
            "reason": self.reason,
            "paid": self.paid,
            "trial": self.trial,
            "use_class": self.use_class,
            "entitlements": list(self.entitlements),
            "source": self.source,
            "license_id": self.license_id,
            "environment": ({"id": self.environment_id, "type": self.environment_type,
                             "name": self.environment_name}
                            if self.environment_id or self.environment_type else None),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "license_expires_at": self.license_expires_at,
            "grace_until": self.grace_until,
            "offline_until": self.offline_until,
            "last_validated": self.last_validated,
        }
        if now is not None:
            out["days_left"] = self.days_left(now)
            out["validity"] = self.validity(now)
        if self.clock_rollback:
            out["clock_rollback"] = True
        return out


def free_state(reason: str = "no_license", *, source: str = "none",
               state: str = "free") -> LicenseState:
    """A state with nothing paid in it. `state` may be expired/revoked/invalid,
    which are Free in behaviour but say why."""
    return LicenseState(state=state, reason=reason, source=source)

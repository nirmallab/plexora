"""Exceptions the licensing package raises.

Everything is a `LicenseError`, so a caller has one type to catch. None of these
ever reaches a Free code path: resolution catches them and turns them into a
state, and only an explicit `plexora license ...` command shows one to a person.
"""

from __future__ import annotations


class LicenseError(Exception):
    """Something about a licence that a person may need to act on."""

    #: A short code, shared with the licence server's error vocabulary where
    #: the failure came from there.
    code = "license_error"

    def __init__(self, message: str, *, code: str | None = None, detail=None):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.detail = detail


class MalformedCertificate(LicenseError):
    code = "malformed"


class UnknownKey(LicenseError):
    code = "unknown_key"


class BadSignature(LicenseError):
    code = "bad_signature"


class WrongProduct(LicenseError):
    code = "wrong_product"


class UnsupportedVersion(LicenseError):
    code = "unsupported_version"


class FutureDated(LicenseError):
    code = "future_dated"


class OfflineRefused(LicenseError):
    """A network call was needed but PLEXORA_LICENSE_OFFLINE forbids it."""

    code = "offline_refused"


class ServerError(LicenseError):
    """The licence server answered with a structured refusal, or not at all.

    `code` is the server's error code (see `client.SERVER_CODES`), or
    `unreachable` / `bad_response` for a transport failure.
    """

    code = "unreachable"

    def __init__(self, message: str, *, code: str | None = None, detail=None,
                 status: int | None = None, retry_after: int | None = None,
                 next_allowed_at: float | None = None):
        super().__init__(message, code=code, detail=detail)
        self.status = status
        self.retry_after = retry_after
        self.next_allowed_at = next_allowed_at

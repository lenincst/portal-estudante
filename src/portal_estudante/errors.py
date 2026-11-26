"""Specific exception hierarchy for the Ulife API client.

Every failure has its own type so callers (and the change detector) can tell *what* broke:
authentication, secrets, a contract change on Ulife's side, or a blocked write action.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class PortalError(Exception):
    """Base class for every error raised by portal_estudante."""


# --- Secrets -----------------------------------------------------------------------------------
class SecretsError(PortalError):
    """Problem reading/writing the local credential store."""


class SecretNotFoundError(SecretsError):
    """A required credential/key is not stored yet."""

    def __init__(self, key: str) -> None:
        super().__init__(f"'{key}' is not stored yet; run `python -m portal_estudante setup` / `login`.")
        self.key = key


# --- Authentication / session ------------------------------------------------------------------
class AuthError(PortalError):
    """Authentication with Ulife / Microsoft Entra ID failed."""


class MfaRequiredError(AuthError):
    """Microsoft asked for a second factor that cannot be completed automatically."""


class SessionExpiredError(AuthError):
    """The Ulife session is no longer valid (redirect to login or 401/403)."""


# --- Contract / change detection ---------------------------------------------------------------
@dataclass
class ContractIssue:
    """One concrete difference between the expected and the observed contract."""

    location: str  # e.g. "GET /api/x -> $.items[0].grade"
    expected: str
    observed: str


class PortalContractError(PortalError):
    """Ulife changed: a response no longer matches the expected contract."""

    def __init__(self, endpoint: str, issues: list[ContractIssue], hint: str = "") -> None:
        self.endpoint = endpoint
        self.issues = issues
        self.hint = hint
        lines = [f"Ulife contract changed at {endpoint}:"]
        lines += [f"  - {i.location}: expected {i.expected}, observed {i.observed}" for i in issues]
        if hint:
            lines.append(f"  What to update: {hint}")
        super().__init__("\n".join(lines))


class EndpointGoneError(PortalContractError):
    """An endpoint that used to exist now returns 404/410 or redirects elsewhere."""


class SchemaChangedError(PortalContractError):
    """A JSON response is missing fields, has new required shapes or changed types."""


class PageStructureChangedError(PortalContractError):
    """An HTML screen no longer has the elements the parser relies on."""


# --- Writes ----------------------------------------------------------------------------------
@dataclass
class WriteBlockedError(PortalError):
    """A write action was attempted without explicit confirmation."""

    action: str
    details: dict[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        return (f"Write action '{self.action}' requires explicit confirmation (confirm=True) "
                f"and is disabled unless explicitly enabled. Details: {self.details}")


class ClassmatesDisabledError(PortalError):
    """The class roster (other students' data) was requested without opting in."""


class RateLimitedError(PortalError):
    """Ulife answered 429 or a throttling page; slow down."""

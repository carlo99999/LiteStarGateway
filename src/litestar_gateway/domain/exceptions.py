"""Domain-level errors, decoupled from any web framework."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for domain errors."""


class InvalidAPIKey(DomainError):
    """The supplied key is missing, unknown, or revoked."""


class APIKeyNotFound(DomainError):
    """No API key exists for the given identifier."""


class InvalidInvite(DomainError):
    """The invite token is unknown or already used."""


class InvalidPasswordReset(DomainError):
    """The password-reset token is unknown, already used, or expired."""


class EmailAlreadyRegistered(DomainError):
    """A user with this email already exists."""


class MasterKeyMissing(DomainError):
    """The users table is empty and no MASTER_KEY was provided to bootstrap admin."""


class InvalidCredentials(DomainError):
    """Login failed: unknown email or wrong password."""


class WeakPassword(DomainError):
    """The chosen password does not meet the minimum complexity policy.

    Backend safety net only — primary complexity feedback is the FE's job.
    """


class PermissionDenied(DomainError):
    """The acting user is not allowed to perform this operation."""


class UserNotFound(DomainError):
    """No user exists for the given identifier/email."""


class InvalidScimToken(DomainError):
    """The SCIM provisioning token is missing, unknown, or revoked."""


class ScimTokenNotFound(DomainError):
    """No SCIM provisioning token exists for the given identifier."""


class SSOIdentityConflict(DomainError):
    """The SSO email already belongs to an account linked to a different IdP
    subject — refuse to let one identity adopt another's account (e.g. a recycled
    corporate email being reassigned at the IdP)."""


class SSOEmailNotVerified(DomainError):
    """The IdP did not assert the email address as verified, so it cannot be
    trusted to resolve or provision a local account."""


class SSOExchangeError(DomainError):
    """The OIDC authorization-code exchange or id_token verification failed
    (misconfigured IdP, network error, missing/invalid token). Surfaced as an
    auth failure rather than leaking the underlying provider error."""


class OrganizationNotFound(DomainError):
    """No organization exists for the given id."""


class TeamNotFound(DomainError):
    """No team exists for the given id."""


class AlreadyMember(DomainError):
    """The user is already a member of the team."""


class MembershipNotFound(DomainError):
    """The user is not a member of the team."""


class LastTeamAdmin(DomainError):
    """The operation would leave the team with no admin (removing/demoting the
    last remaining admin), orphaning team-level management."""


class CredentialNotFound(DomainError):
    """No credential exists for the given id."""


class CredentialNameExists(DomainError):
    """A credential with this name already exists."""


class SaltKeyMissing(DomainError):
    """SALT_KEY is not configured; credential encryption is unavailable."""


class CredentialMisconfigured(DomainError):
    """The credential is missing a value required to call the provider (e.g. api_key)."""


class ModelNotFound(DomainError):
    """No model exists for the given id (within the team)."""


class ModelNameExists(DomainError):
    """A model with this name already exists in the team."""


class ProviderMismatch(DomainError):
    """The model's provider does not match the referenced credential's provider."""


class ModelDisabled(DomainError):
    """The model exists but is disabled and cannot be invoked."""


class UnsupportedOperation(DomainError):
    """This provider does not support the requested operation (e.g. /responses)."""


class ModelTypeMismatch(DomainError):
    """The model's type does not match the requested operation (e.g. chat vs embeddings)."""


class ServicePrincipalNotFound(DomainError):
    """No such service principal in this team."""


class InvalidServicePrincipal(DomainError):
    """The service-principal definition is invalid (e.g. empty/too-long name)."""


class ManagementScopeRequiresServicePrincipal(DomainError):
    """A personal key cannot hold management/all scope — use a service principal."""


class InvalidKeyScope(DomainError):
    """The requested API-key scope is not one of inference/management/all."""


class BudgetExceeded(DomainError):
    """The team's spend cap for the current window is exhausted (→ 402)."""


class BudgetNotFound(DomainError):
    """The team has no budget configured."""


class InvalidBudget(DomainError):
    """The budget definition is invalid (non-positive limit or unknown window)."""


class UpstreamError(DomainError):
    """Base for provider-side failures surfaced by the gateway (not gateway bugs)."""


class UpstreamRateLimited(UpstreamError):
    """The provider rate-limited the request (429). `retry_after` carries the
    provider's Retry-After header value, when present, so clients can back off."""

    def __init__(self, message: str, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class UpstreamAuthFailed(UpstreamError):
    """The provider rejected the gateway's credential (401/403): expired or
    rotated upstream key. An ops problem (-> 502), never the client's fault."""


class UpstreamRequestRejected(UpstreamError):
    """The provider refused the request itself (other 4xx, e.g. an
    out-of-range parameter passed through the allowlist) -> 400."""


class UpstreamUnavailable(UpstreamError):
    """The provider returned a 5xx or could not be reached."""


class UpstreamTimeout(UpstreamError):
    """The provider did not respond within the configured timeout."""

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


class OrganizationNotFound(DomainError):
    """No organization exists for the given id."""


class TeamNotFound(DomainError):
    """No team exists for the given id."""


class AlreadyMember(DomainError):
    """The user is already a member of the team."""


class MembershipNotFound(DomainError):
    """The user is not a member of the team."""


class CredentialNotFound(DomainError):
    """No credential exists for the given id."""


class CredentialNameExists(DomainError):
    """A credential with this name already exists."""


class SaltKeyMissing(DomainError):
    """SALT_KEY is not configured; credential encryption is unavailable."""


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

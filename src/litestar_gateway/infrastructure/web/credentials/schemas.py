"""DTOs for provider credentials (OpenAPI-documented)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

from litestar.params import Parameter

from litestar_gateway.domain.entities import Credential, Provider


@dataclass(frozen=True)
class CreateCredentialRequest:
    name: Annotated[
        str,
        Parameter(description="Unique, human-readable name, e.g. `prod-openai`."),
    ]
    provider: Annotated[
        Provider,
        Parameter(description="The LLM provider this credential connects to."),
    ]
    values: Annotated[
        dict[str, str],
        Parameter(
            description=(
                "Provider-specific secret fields (see the endpoint description for "
                "the expected keys per provider). Stored **encrypted at rest** with "
                "the salt key and never returned by any endpoint."
            ),
        ),
    ]


@dataclass(frozen=True)
class UpdateCredentialRequest:
    """Rename a credential and/or replace its secret values. Both optional;
    omitted/null fields are left unchanged. The provider is immutable."""

    name: Annotated[
        str | None,
        Parameter(description="New unique name. Omit to keep the current one."),
    ] = None
    values: Annotated[
        dict[str, str] | None,
        Parameter(
            description=(
                "A full fresh set of the provider's secret fields (e.g. a rotated "
                "token). Omit to keep the current values — they are never revealed, "
                "so this replaces, it does not merge."
            ),
        ),
    ] = None


@dataclass(frozen=True)
class CredentialResponse:
    """Credential metadata. Secret values are intentionally never included."""

    id: UUID
    name: str
    provider: Provider
    created_at: datetime

    @classmethod
    def from_entity(cls, credential: Credential) -> CredentialResponse:
        return cls(
            id=credential.id,
            name=credential.name,
            provider=credential.provider,
            created_at=credential.created_at,
        )

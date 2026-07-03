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

"""Provider credential management — platform-admin only.

Credentials hold the secrets needed to connect to an LLM provider. Secret
``values`` are encrypted at rest with the salt key and are **never** returned by
any endpoint; only metadata (id, name, provider, timestamps) is readable.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from litestar import Controller, delete, get, post
from litestar.di import NamedDependency, Provide
from litestar.openapi.spec import Example
from litestar.params import Body, FromPath, FromQuery

from litestar_test.application.credential_service import CredentialService
from litestar_test.domain.entities import User
from litestar_test.domain.pagination import resolve_page
from litestar_test.infrastructure.web.credentials.schemas import (
    CreateCredentialRequest,
    CredentialResponse,
)
from litestar_test.infrastructure.web.session.dependencies import provide_current_admin

# Documents the expected `values` keys per provider (mirrors litellm field names).
_CREATE_DESCRIPTION = """\
Create a provider credential. The secret `values` are encrypted at rest with the
salt key and can never be read back — only metadata is returned.

**Expected `values` keys per provider** (optional keys in parentheses):

- `openai`: `api_key` (`api_base`, `organization`)
- `anthropic`: `api_key` (`api_base`)
- `azure_openai`: `api_key`, `api_base`, `api_version` (`deployment`)
- `vertex_ai`: `vertex_project`, `vertex_location`, `vertex_credentials`
- `bedrock`: `aws_access_key_id`, `aws_secret_access_key`, `aws_region_name`
  (`aws_session_token`)
- `databricks`: `api_key`, `api_base`

Requires a platform-admin JWT.
"""

_CREATE_EXAMPLES = [
    Example(
        summary="OpenAI",
        value={"name": "prod-openai", "provider": "openai", "values": {"api_key": "sk-..."}},
    ),
    Example(
        summary="AWS Bedrock",
        value={
            "name": "prod-bedrock",
            "provider": "bedrock",
            "values": {
                "aws_access_key_id": "AKIA...",
                "aws_secret_access_key": "...",
                "aws_region_name": "us-east-1",
            },
        },
    ),
    Example(
        summary="Vertex AI",
        value={
            "name": "prod-vertex",
            "provider": "vertex_ai",
            "values": {
                "vertex_project": "my-gcp-project",
                "vertex_location": "us-central1",
                "vertex_credentials": "{...service account json...}",
            },
        },
    ),
]


class CredentialController(Controller):
    path = "/credentials"
    tags = ["credentials"]
    # Platform-admin only (User.is_admin).
    dependencies = {"current_admin": Provide(provide_current_admin)}

    @post(
        summary="Create a provider credential",
        description=_CREATE_DESCRIPTION,
    )
    async def create_credential(
        self,
        data: Annotated[
            CreateCredentialRequest,
            Body(
                title="Credential to create",
                description="Provider, a unique name, and the secret values to encrypt.",
                examples=_CREATE_EXAMPLES,
            ),
        ],
        current_admin: NamedDependency[User],
        credential_service: NamedDependency[CredentialService],
    ) -> CredentialResponse:
        credential = await credential_service.create(
            current_admin, data.name, data.provider, data.values
        )
        return CredentialResponse.from_entity(credential)

    @get(
        summary="List provider credentials",
        description="Returns credential metadata only — never the secret values.",
    )
    async def list_credentials(
        self,
        current_admin: NamedDependency[User],
        credential_service: NamedDependency[CredentialService],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[CredentialResponse]:
        page_limit, page_offset = resolve_page(limit, offset)
        credentials = await credential_service.list(
            current_admin, limit=page_limit, offset=page_offset
        )
        return [CredentialResponse.from_entity(c) for c in credentials]

    @delete(
        "/{credential_id:uuid}",
        summary="Delete a provider credential",
        description="Permanently removes the credential and its encrypted values.",
    )
    async def delete_credential(
        self,
        credential_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        credential_service: NamedDependency[CredentialService],
    ) -> None:
        await credential_service.delete(current_admin, credential_id)

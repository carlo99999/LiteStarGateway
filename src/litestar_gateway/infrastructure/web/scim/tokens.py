"""Admin endpoints to mint/list/revoke SCIM provisioning tokens (admin JWT)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from litestar import Request, delete, get, post
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath

from litestar_gateway.application.scim_service import ScimService
from litestar_gateway.domain.entities import IssuedScimToken, ScimToken, User
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_admin


@dataclass(frozen=True)
class ScimTokenCreateRequest:
    name: str


@dataclass(frozen=True)
class ScimTokenCreatedResponse:
    """Returned once at creation. `token` is never retrievable again."""

    id: UUID
    name: str
    token: str
    created_at: datetime

    @classmethod
    def from_issued(cls, issued: IssuedScimToken) -> ScimTokenCreatedResponse:
        return cls(
            id=issued.scim_token.id,
            name=issued.scim_token.name,
            token=issued.token,
            created_at=issued.scim_token.created_at,
        )


@dataclass(frozen=True)
class ScimTokenResponse:
    id: UUID
    name: str
    created_at: datetime
    revoked_at: datetime | None

    @classmethod
    def from_entity(cls, token: ScimToken) -> ScimTokenResponse:
        return cls(
            id=token.id,
            name=token.name,
            created_at=token.created_at,
            revoked_at=token.revoked_at,
        )


@post("/scim-tokens", dependencies={"admin_user": Provide(provide_current_admin)})
async def create_scim_token(
    request: Request,
    data: ScimTokenCreateRequest,
    admin_user: NamedDependency[User],
    scim_service: NamedDependency[ScimService],
    audit_log: NamedDependency[AuditLog],
) -> ScimTokenCreatedResponse:
    issued = await scim_service.create_token(admin_user, data.name)
    await record_audit(
        audit_log,
        request,
        admin_user,
        "scim_token.create",
        target_type="scim_token",
        target_id=issued.scim_token.id,
        detail=issued.scim_token.name,
    )
    return ScimTokenCreatedResponse.from_issued(issued)


@get("/scim-tokens", dependencies={"admin_user": Provide(provide_current_admin)})
async def list_scim_tokens(
    admin_user: NamedDependency[User],
    scim_service: NamedDependency[ScimService],
) -> list[ScimTokenResponse]:
    tokens = await scim_service.list_tokens(admin_user)
    return [ScimTokenResponse.from_entity(t) for t in tokens]


@delete("/scim-tokens/{token_id:uuid}", dependencies={"admin_user": Provide(provide_current_admin)})
async def revoke_scim_token(
    request: Request,
    token_id: FromPath[UUID],
    admin_user: NamedDependency[User],
    scim_service: NamedDependency[ScimService],
    audit_log: NamedDependency[AuditLog],
) -> None:
    await scim_service.revoke_token(admin_user, token_id)
    await record_audit(
        audit_log,
        request,
        admin_user,
        "scim_token.revoke",
        target_type="scim_token",
        target_id=token_id,
    )

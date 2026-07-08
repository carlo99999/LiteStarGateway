"""IdP-facing SCIM 2.0 endpoints: /scim/v2/Users + ServiceProviderConfig.

Handlers parse bodies via `request.json()` rather than typed DTOs: IdPs send
`Content-Type: application/scim+json` and sparse/extra attributes, both of which
typed body binding would reject. Responses use the SCIM media type, and domain
errors are rendered as RFC 7644 Error resources by the router-level handler.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from litestar import Request, Response, delete, get, patch, post, put
from litestar.di import NamedDependency
from litestar.params import FromPath, FromQuery, QueryParameter
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_400_BAD_REQUEST,
    HTTP_403_FORBIDDEN,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
)

from litestar_gateway.application.scim_service import ScimService
from litestar_gateway.domain.entities import AuditEvent, ScimToken
from litestar_gateway.domain.exceptions import (
    DomainError,
    EmailAlreadyRegistered,
    PermissionDenied,
    UserNotFound,
)
from litestar_gateway.domain.pagination import resolve_page
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.web.scim.schemas import (
    SCIM_MEDIA_TYPE,
    ScimUserAttrs,
    apply_patch_ops,
    parse_filter,
    parse_user_payload,
    scim_error,
    scim_list_response,
    scim_user_resource,
)

# Most specific first; matched by isinstance (mirrors the app-level handler, but
# renders RFC 7644 Error resources instead of the gateway's {"detail": ...}).
_SCIM_STATUS: list[tuple[type[DomainError], int, str | None]] = [
    (PermissionDenied, HTTP_403_FORBIDDEN, None),
    (UserNotFound, HTTP_404_NOT_FOUND, None),
    (EmailAlreadyRegistered, HTTP_409_CONFLICT, "uniqueness"),
]


def scim_domain_exception_handler(_: Request, exc: DomainError) -> Response:
    for cls, status, scim_type in _SCIM_STATUS:
        if isinstance(exc, cls):
            detail = str(exc) or exc.__class__.__name__
            return _scim_response(scim_error(status, detail, scim_type), status)
    return _scim_response(
        scim_error(HTTP_400_BAD_REQUEST, str(exc) or exc.__class__.__name__),
        HTTP_400_BAD_REQUEST,
    )


def _scim_response(content: dict[str, Any], status: int = HTTP_200_OK) -> Response:
    return Response(content, status_code=status, media_type=SCIM_MEDIA_TYPE)


def _bad_request(detail: str, scim_type: str) -> Response:
    return _scim_response(scim_error(HTTP_400_BAD_REQUEST, detail, scim_type), 400)


async def _record_scim_audit(
    audit_log: AuditLog,
    request: Request,
    token: ScimToken,
    action: str,
    target_id: UUID,
) -> None:
    """SCIM's actor is the provisioning token (the IdP), not a user/Principal."""
    await audit_log.record(
        AuditEvent(
            id=uuid4(),
            action=action,
            actor_id=token.id,
            actor_type="scim_token",
            actor_email=f"scim:{token.name}",
            target_type="user",
            target_id=str(target_id),
            ip=request.client.host if request.client else None,
            detail=None,
            created_at=datetime.now(UTC),
        )
    )


def _update_action(was_active: bool, now_active: bool) -> str:
    """Audit action for a PUT/PATCH: lifecycle flips get their own action so an
    IdP (re)enabling or disabling an account is distinguishable from attribute
    churn in the audit trail."""
    if was_active and not now_active:
        return "scim.user.deactivate"
    if not was_active and now_active:
        return "scim.user.reactivate"
    return "scim.user.update"


@get("/ServiceProviderConfig")
async def service_provider_config(scim_actor: NamedDependency[ScimToken]) -> Response:
    """Capability discovery: Users only — no bulk, no sorting, no /Groups
    (team membership is governed in the gateway, per the enterprise design)."""
    return _scim_response(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 500},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "Bearer token",
                    "description": "Admin-minted SCIM provisioning token",
                }
            ],
        }
    )


@post("/Users")
async def create_scim_user(
    request: Request,
    scim_actor: NamedDependency[ScimToken],
    scim_service: NamedDependency[ScimService],
    audit_log: NamedDependency[AuditLog],
) -> Response:
    try:
        attrs = parse_user_payload(await request.json())
    except ValueError as exc:
        return _bad_request(str(exc), "invalidValue")
    user = await scim_service.create_user(
        email=attrs.user_name,  # type: ignore[arg-type]  # non-None: userName required
        external_id=attrs.external_id,
        active=attrs.active,
    )
    await _record_scim_audit(audit_log, request, scim_actor, "scim.user.create", user.id)
    return _scim_response(scim_user_resource(user), HTTP_201_CREATED)


@get("/Users")
async def list_scim_users(
    scim_actor: NamedDependency[ScimToken],
    scim_service: NamedDependency[ScimService],
    scim_filter: Annotated[str | None, QueryParameter(name="filter")] = None,
    start_index: Annotated[int | None, QueryParameter(name="startIndex")] = None,
    count: FromQuery[int | None] = None,
) -> Response:
    email = external_id = None
    if scim_filter is not None:
        try:
            attribute, value = parse_filter(scim_filter)
        except ValueError as exc:
            return _bad_request(str(exc), "invalidFilter")
        if attribute == "userName":
            email = value
        else:
            external_id = value
    limit, offset = resolve_page(count, (start_index - 1) if start_index else 0)
    users, total = await scim_service.find_users(
        email=email, external_id=external_id, offset=offset, limit=limit
    )
    return _scim_response(
        scim_list_response(
            [scim_user_resource(u) for u in users],
            total=total,
            start_index=max(1, start_index or 1),
        )
    )


@get("/Users/{user_id:uuid}")
async def get_scim_user(
    user_id: FromPath[UUID],
    scim_actor: NamedDependency[ScimToken],
    scim_service: NamedDependency[ScimService],
) -> Response:
    return _scim_response(scim_user_resource(await scim_service.get_user(user_id)))


@put("/Users/{user_id:uuid}", status_code=HTTP_200_OK)
async def replace_scim_user(
    request: Request,
    user_id: FromPath[UUID],
    scim_actor: NamedDependency[ScimToken],
    scim_service: NamedDependency[ScimService],
    audit_log: NamedDependency[AuditLog],
) -> Response:
    try:
        attrs = parse_user_payload(await request.json())
    except ValueError as exc:
        return _bad_request(str(exc), "invalidValue")
    was_active = (await scim_service.get_user(user_id)).is_active
    user = await scim_service.update_user(
        user_id, email=attrs.user_name, external_id=attrs.external_id, active=attrs.active
    )
    action = _update_action(was_active, user.is_active)
    await _record_scim_audit(audit_log, request, scim_actor, action, user.id)
    return _scim_response(scim_user_resource(user))


@patch("/Users/{user_id:uuid}", status_code=HTTP_200_OK)
async def patch_scim_user(
    request: Request,
    user_id: FromPath[UUID],
    scim_actor: NamedDependency[ScimToken],
    scim_service: NamedDependency[ScimService],
    audit_log: NamedDependency[AuditLog],
) -> Response:
    user = await scim_service.get_user(user_id)
    current = ScimUserAttrs(
        user_name=user.email, external_id=user.external_id, active=user.is_active
    )
    try:
        payload = await request.json()
        operations = payload.get("Operations")
        if not isinstance(operations, list):
            raise ValueError("Operations must be a list")
        desired = apply_patch_ops(current, operations)
    except ValueError as exc:
        return _bad_request(str(exc), "invalidValue")
    updated = await scim_service.update_user(
        user_id, email=desired.user_name, external_id=desired.external_id, active=desired.active
    )
    action = _update_action(user.is_active, updated.is_active)
    await _record_scim_audit(audit_log, request, scim_actor, action, updated.id)
    return _scim_response(scim_user_resource(updated))


@delete("/Users/{user_id:uuid}")
async def delete_scim_user(
    request: Request,
    user_id: FromPath[UUID],
    scim_actor: NamedDependency[ScimToken],
    scim_service: NamedDependency[ScimService],
    audit_log: NamedDependency[AuditLog],
) -> None:
    """SCIM DELETE deactivates (soft): audit/usage history must keep its actor."""
    await scim_service.deactivate_user(user_id)
    await _record_scim_audit(audit_log, request, scim_actor, "scim.user.deactivate", user_id)

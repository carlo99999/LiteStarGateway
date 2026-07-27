"""Team membership management + team-scoped API keys.

Authorization is enforced by `TeamService.ensure_team_permission` (platform
admin or team admin). Domain errors are mapped to HTTP by the central handler.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parseaddr
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from litestar import Controller, Request, delete, get, patch, post, put
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath, FromQuery

from litestar_gateway.application.routing.webhook import _is_blocked, _literal_ip
from litestar_gateway.application.service import APIKeyService
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.domain.authorization import Permission
from litestar_gateway.domain.budget import validate_thresholds, window_start
from litestar_gateway.domain.entities import (
    Budget,
    BudgetWindow,
    KeyScope,
    Principal,
    UsageTimeseries,
    User,
)
from litestar_gateway.domain.exceptions import (
    BudgetNotFound,
    InvalidBudget,
    InvalidKeyScope,
    InvalidUsageQuery,
)
from litestar_gateway.domain.pagination import resolve_page
from litestar_gateway.domain.ports import (
    AuditLog,
    BudgetAlertStateRepository,
    BudgetRepository,
    UsageRepository,
)
from litestar_gateway.infrastructure.web.audit.recorder import make_audit_event, record_audit
from litestar_gateway.infrastructure.web.principal import provide_principal
from litestar_gateway.infrastructure.web.session.dependencies import (
    provide_current_admin,
    provide_current_user,
)
from litestar_gateway.infrastructure.web.teams.schemas import (
    AddMemberRequest,
    BudgetAlertResponse,
    BudgetResponse,
    CreatedKeyResponse,
    CreateKeyRequest,
    KeyResponse,
    KeySpendingResponse,
    MembershipResponse,
    SetBudgetRequest,
    SetRoleRequest,
    TeamResponse,
    UpdateTeamRequest,
    UsageResponse,
    UsageTimeseriesResponse,
    resolve_key_expiry,
)

_TIMESERIES_GRANULARITIES = frozenset({"hour", "day"})


def _cache_savings_response(
    avoided_cost: float, priced_hits: int, hits_without_price: int, total_events: int
) -> dict[str, Any]:
    """Shared shape for the team-scoped and platform-wide cache-savings
    endpoints (Plan 04 Phase 3), mirroring the routing savings dict shape."""
    hits = priced_hits + hits_without_price
    return {
        "cache_hit_rate": (hits / total_events) if total_events else 0.0,
        "estimated_cost_saved": avoided_cost,
        "cache_hits": hits,
        "cache_hits_without_price": hits_without_price,
        "total_requests": total_events,
    }


def _validate_alert_webhook_url(url: str) -> str:
    """Boundary-validate an operator-supplied per-team alert webhook URL with
    the SAME SSRF deny-list the send path uses (Plan 07 Phase 2/3): an
    http(s) URL whose host is not a private/loopback/link-local literal IP.
    A hostname is re-resolved and re-checked on every send by the channel
    (DNS-rebinding guard); this is the config-time literal check that mirrors
    `WebhookNotificationChannel.__init__`."""
    if not url.startswith(("http://", "https://")):
        raise InvalidBudget("alert_webhook_url must be an http(s) URL")
    host = urlsplit(url).hostname
    if not host:
        raise InvalidBudget("alert_webhook_url has no host")
    literal = _literal_ip(host)
    if literal is not None and _is_blocked(literal):
        raise InvalidBudget(
            "alert_webhook_url targets a private/loopback/link-local address; "
            "only public endpoints are allowed"
        )
    return url


def _validate_alert_email(value: str) -> str:
    """A basic sanity check on an alert recipient — NOT the security boundary
    (email is not an egress target). Rejects obviously malformed addresses."""
    address = parseaddr(value)[1]
    local, _, domain = address.partition("@")
    if not local or "." not in domain:
        raise InvalidBudget("alert_email must be a valid email address")
    return address


def _parse_budget(data: SetBudgetRequest, team_id: UUID) -> Budget:
    if data.limit_cost <= 0:
        raise InvalidBudget("limit_cost must be a positive USD amount")
    try:
        window = BudgetWindow(data.window)
    except ValueError:
        valid = ", ".join(w.value for w in BudgetWindow)
        raise InvalidBudget(f"window must be one of: {valid}") from None
    thresholds = validate_thresholds(data.thresholds or [])
    webhook_url = (
        _validate_alert_webhook_url(data.alert_webhook_url) if data.alert_webhook_url else None
    )
    email = _validate_alert_email(data.alert_email) if data.alert_email else None
    return Budget(
        id=uuid4(),
        team_id=team_id,
        limit_cost=data.limit_cost,
        window=window,
        created_at=datetime.now(UTC),
        thresholds=thresholds,
        alert_webhook_url=webhook_url,
        alert_email=email,
    )


class TeamController(Controller):
    path = "/teams"
    tags = ["teams"]
    # Any authenticated user; per-team authorization happens in the service.
    dependencies = {"current_user": Provide(provide_current_user)}

    @get("")
    async def list_teams(
        self,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[TeamResponse]:
        """Every team across all organizations (platform-admin only)."""
        page_limit, page_offset = resolve_page(limit, offset)
        teams = await team_service.list_all_teams(
            current_user, limit=page_limit, offset=page_offset
        )
        return [TeamResponse.from_entity(t) for t in teams]

    @get("/{team_id:uuid}")
    async def get_team(
        self,
        team_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
    ) -> TeamResponse:
        """One team by id (platform admin, auditor, or a team member with read)."""
        team = await team_service.get_team(current_user, team_id)
        return TeamResponse.from_entity(team)

    @patch("/{team_id:uuid}", dependencies={"current_admin": Provide(provide_current_admin)})
    async def update_team(
        self,
        request: Request,
        team_id: FromPath[UUID],
        data: UpdateTeamRequest,
        current_admin: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        audit_log: NamedDependency[AuditLog],
    ) -> TeamResponse:
        team = await team_service.update_team(
            current_admin,
            team_id,
            data.name,
            description=data.description,
            tags=data.tags,
            rate_limit_rpm=data.rate_limit_rpm,
        )
        await record_audit(
            audit_log,
            request,
            current_admin,
            "team.update",
            target_type="team",
            target_id=team.id,
            detail=data.name,
        )
        return TeamResponse.from_entity(team)

    @delete("/{team_id:uuid}", dependencies={"current_admin": Provide(provide_current_admin)})
    async def delete_team(
        self,
        request: Request,
        team_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        # Refuses with 409 (TeamNotEmpty) if the team still has models or API
        # keys; otherwise removes the team and its intrinsic children.
        team = await team_service.delete_team(current_admin, team_id)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "team.delete",
            target_type="team",
            target_id=team.id,
            detail=team.name,
        )

    @get("/{team_id:uuid}/members")
    async def list_members(
        self,
        team_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[MembershipResponse]:
        page_limit, page_offset = resolve_page(limit, offset)
        members = await team_service.list_members(
            current_user, team_id, limit=page_limit, offset=page_offset
        )
        return [MembershipResponse.from_entity(m) for m in members]

    @post("/{team_id:uuid}/members")
    async def add_member(
        self,
        request: Request,
        team_id: FromPath[UUID],
        data: AddMemberRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        audit_log: NamedDependency[AuditLog],
    ) -> MembershipResponse:
        membership = await team_service.add_member(current_user, team_id, data.email, data.role)
        await record_audit(
            audit_log,
            request,
            current_user,
            "team.member.add",
            target_type="team",
            target_id=team_id,
            detail=f"{data.email} as {data.role}",
        )
        return MembershipResponse.from_entity(membership)

    @patch("/{team_id:uuid}/members/{user_id:uuid}")
    async def set_member_role(
        self,
        request: Request,
        team_id: FromPath[UUID],
        user_id: FromPath[UUID],
        data: SetRoleRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        audit_log: NamedDependency[AuditLog],
    ) -> MembershipResponse:
        membership = await team_service.set_role(current_user, team_id, user_id, data.role)
        await record_audit(
            audit_log,
            request,
            current_user,
            "team.member.set_role",
            target_type="team",
            target_id=team_id,
            detail=f"user {user_id} -> {data.role}",
        )
        return MembershipResponse.from_entity(membership)

    @delete("/{team_id:uuid}/members/{user_id:uuid}")
    async def remove_member(
        self,
        request: Request,
        team_id: FromPath[UUID],
        user_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        await team_service.remove_member(current_user, team_id, user_id)
        await record_audit(
            audit_log,
            request,
            current_user,
            "team.member.remove",
            target_type="team",
            target_id=team_id,
            detail=f"user {user_id}",
        )

    @post("/{team_id:uuid}/keys")
    async def create_key(
        self,
        request: Request,
        team_id: FromPath[UUID],
        data: CreateKeyRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        api_key_service: NamedDependency[APIKeyService],
        audit_log: NamedDependency[AuditLog],
    ) -> CreatedKeyResponse:
        await team_service.ensure_team_permission(current_user, team_id, Permission.KEYS_ISSUE)
        try:
            scope = KeyScope(data.scope)
        except ValueError:
            valid = ", ".join(s.value for s in KeyScope)
            raise InvalidKeyScope(f"scope must be one of: {valid}") from None
        issued = await api_key_service.issue(
            team_id=team_id,
            created_by=current_user.id,
            name=data.name,
            scope=scope,
            rate_limit_rpm=data.rate_limit_rpm,
            expires_at=resolve_key_expiry(data.expires_in_days),
        )
        await record_audit(
            audit_log,
            request,
            current_user,
            "api_key.create",
            target_type="api_key",
            target_id=issued.key.id,
            detail=f"team {team_id}",
        )
        return CreatedKeyResponse.from_issued(issued)

    @get("/{team_id:uuid}/keys")
    async def list_keys(
        self,
        team_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        api_key_service: NamedDependency[APIKeyService],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[KeyResponse]:
        await team_service.ensure_team_permission(current_user, team_id, Permission.KEYS_READ)
        page_limit, page_offset = resolve_page(limit, offset)
        keys = await api_key_service.list_for_team(team_id, limit=page_limit, offset=page_offset)
        return [KeyResponse.from_entity(k) for k in keys]

    @get(
        "/{team_id:uuid}/keys/spending",
        summary="API keys (incl. revoked) with their spend",
        dependencies={"principal": Provide(provide_principal)},
    )
    async def keys_spending(
        self,
        team_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        team_service: NamedDependency[TeamService],
        api_key_service: NamedDependency[APIKeyService],
        usage_repository: NamedDependency[UsageRepository],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[KeySpendingResponse]:
        """Every API key of the team — active and revoked — with its accumulated
        token/cost totals, so past keys and their spend stay visible.
        Accepts a JWT or a management-scoped API key (own team only).
        Callers without keys:read get the key-identity block redacted (R6-M43)."""
        await team_service.ensure_principal_team_permission(
            principal, team_id, Permission.USAGE_READ
        )
        include_identity = await team_service.principal_has_team_permission(
            principal, team_id, Permission.KEYS_READ
        )
        page_limit, page_offset = resolve_page(limit, offset)
        keys = await api_key_service.list_for_team(team_id, limit=page_limit, offset=page_offset)
        spend = {s.api_key_id: s for s in await usage_repository.spend_by_api_key(team_id)}
        return [
            KeySpendingResponse.from_key_and_spend(
                k, spend.get(k.id), include_identity=include_identity
            )
            for k in keys
        ]

    @delete("/{team_id:uuid}/keys/{key_id:uuid}")
    async def revoke_key(
        self,
        request: Request,
        team_id: FromPath[UUID],
        key_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        api_key_service: NamedDependency[APIKeyService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        await team_service.ensure_team_permission(current_user, team_id, Permission.KEYS_ISSUE)
        await api_key_service.revoke_for_team(team_id, key_id)
        await record_audit(
            audit_log,
            request,
            current_user,
            "api_key.revoke",
            target_type="api_key",
            target_id=key_id,
            detail=f"team {team_id}",
        )

    @post("/{team_id:uuid}/keys/{key_id:uuid}/rotate")
    async def rotate_key(
        self,
        request: Request,
        team_id: FromPath[UUID],
        key_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        api_key_service: NamedDependency[APIKeyService],
    ) -> CreatedKeyResponse:
        """Issue a replacement key (same scope/rate-limit/owner) and give the old
        one a grace window before it stops working. Returns the new plaintext once."""
        await team_service.ensure_team_permission(current_user, team_id, Permission.KEYS_ISSUE)
        key = await api_key_service.get_active_for_team(team_id, key_id)
        if key.is_service_principal:
            await team_service.ensure_team_permission(
                current_user, team_id, Permission.SERVICE_PRINCIPALS_MANAGE
            )
        audit_event = make_audit_event(
            request,
            current_user,
            "api_key.rotate",
            target_type="api_key",
            target_id=key_id,
            detail=f"team {team_id}",
        )
        issued = await api_key_service.rotate_for_team(team_id, key_id, audit_event=audit_event)
        return CreatedKeyResponse.from_issued(issued)

    @get("/{team_id:uuid}/budget", dependencies={"principal": Provide(provide_principal)})
    async def get_budget(
        self,
        team_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        team_service: NamedDependency[TeamService],
        budget_repository: NamedDependency[BudgetRepository],
        usage_repository: NamedDependency[UsageRepository],
    ) -> BudgetResponse:
        """The team's spend cap plus its current-window spend and remainder.
        Accepts a JWT or a management-scoped API key (own team only)."""
        await team_service.ensure_principal_team_permission(
            principal, team_id, Permission.BUDGET_READ
        )
        budget = await budget_repository.get(team_id)
        if budget is None:
            raise BudgetNotFound(f"Team {team_id} has no budget configured")
        spent = await usage_repository.spend_since(
            team_id, window_start(budget.window, datetime.now(UTC))
        )
        return BudgetResponse.from_budget(budget, spent)

    @get(
        "/{team_id:uuid}/budget/alerts",
        dependencies={"principal": Provide(provide_principal)},
    )
    async def budget_alerts(
        self,
        team_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        team_service: NamedDependency[TeamService],
        budget_alert_state_repository: NamedDependency[BudgetAlertStateRepository],
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[BudgetAlertResponse]:
        """The team's most-recently fired budget-threshold alerts, newest-first
        (Plan 07 Phase 3, design §8). Same read gate as `get_budget`
        (`BUDGET_READ`); accepts a JWT or a management-scoped API key (own team
        only). Read from the `budget_alert_state` dedup ledger."""
        await team_service.ensure_principal_team_permission(
            principal, team_id, Permission.BUDGET_READ
        )
        page_limit, _ = resolve_page(limit, offset)
        alerts = await budget_alert_state_repository.recent_fired(team_id, limit=page_limit)
        return [BudgetAlertResponse.from_entity(a) for a in alerts]

    @put(
        "/{team_id:uuid}/budget",
        dependencies={"current_admin": Provide(provide_current_admin)},
    )
    async def set_budget(
        self,
        request: Request,
        team_id: FromPath[UUID],
        data: SetBudgetRequest,
        current_admin: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        budget_repository: NamedDependency[BudgetRepository],
        usage_repository: NamedDependency[UsageRepository],
        audit_log: NamedDependency[AuditLog],
    ) -> BudgetResponse:
        """Create or replace the team's spend cap. Platform-admin only — a team
        admin must not be able to raise their own limit."""
        await team_service.ensure_team_permission(
            current_admin, team_id, Permission.BUDGET_READ
        )  # team must exist
        budget = await budget_repository.set(_parse_budget(data, team_id))
        await record_audit(
            audit_log,
            request,
            current_admin,
            "team.budget.set",
            target_type="team",
            target_id=team_id,
            detail=f"{budget.limit_cost} USD / {budget.window.value}",
        )
        spent = await usage_repository.spend_since(
            team_id, window_start(budget.window, datetime.now(UTC))
        )
        return BudgetResponse.from_budget(budget, spent)

    @delete(
        "/{team_id:uuid}/budget",
        dependencies={"current_admin": Provide(provide_current_admin)},
    )
    async def delete_budget(
        self,
        request: Request,
        team_id: FromPath[UUID],
        current_admin: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        budget_repository: NamedDependency[BudgetRepository],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        """Remove the team's spend cap. Platform-admin only."""
        await team_service.ensure_team_permission(current_admin, team_id, Permission.BUDGET_READ)
        await budget_repository.remove(team_id)
        await record_audit(
            audit_log,
            request,
            current_admin,
            "team.budget.remove",
            target_type="team",
            target_id=team_id,
        )

    @get("/{team_id:uuid}/usage", dependencies={"principal": Provide(provide_principal)})
    async def usage(
        self,
        team_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        team_service: NamedDependency[TeamService],
        usage_repository: NamedDependency[UsageRepository],
        model: FromQuery[str | None] = None,
        alias: FromQuery[str | None] = None,
        resolved_model_id: FromQuery[UUID | None] = None,
        api_key_id: FromQuery[UUID | None] = None,
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[UsageResponse]:
        """Per-callable token/cost totals. ``model`` matches requested alias or
        canonical name; ``alias`` and ``resolved_model_id`` are exact filters.
        Optional `?api_key_id=` filters by caller; unfiltered returns all rows, paged.
        Accepts a JWT or a management-scoped API key (own team only)."""
        await team_service.ensure_principal_team_permission(
            principal, team_id, Permission.USAGE_READ
        )
        page_limit, page_offset = resolve_page(limit, offset)
        aggregates = await usage_repository.aggregate(
            team_id,
            model_name=model,
            requested_alias=alias,
            resolved_model_id=resolved_model_id,
            api_key_id=api_key_id,
            limit=page_limit,
            offset=page_offset,
        )
        return [UsageResponse.from_aggregate(a) for a in aggregates]

    @get(
        "/{team_id:uuid}/usage/timeseries",
        dependencies={"principal": Provide(provide_principal)},
    )
    async def usage_timeseries(
        self,
        team_id: FromPath[UUID],
        principal: NamedDependency[Principal],
        team_service: NamedDependency[TeamService],
        usage_repository: NamedDependency[UsageRepository],
        start: FromQuery[datetime],
        end: FromQuery[datetime],
        granularity: FromQuery[str] = "day",
        model: FromQuery[str | None] = None,
        alias: FromQuery[str | None] = None,
        api_key_id: FromQuery[UUID | None] = None,
    ) -> UsageTimeseriesResponse:
        """Bucketed usage over ``[start, end)`` (Plan 10 Phase 1) — the data
        layer the console's per-model-over-time chart will consume. Same
        filter semantics as `usage` (``model`` = alias-or-canonical match,
        ``alias``/``api_key_id`` exact); ``granularity`` is ``hour`` or
        ``day``. Accepts a JWT or a management-scoped API key (own team only),
        same as `usage`."""
        await team_service.ensure_principal_team_permission(
            principal, team_id, Permission.USAGE_READ
        )
        if granularity not in _TIMESERIES_GRANULARITIES:
            valid = sorted(_TIMESERIES_GRANULARITIES)
            raise InvalidUsageQuery(f"granularity must be one of {valid}, got {granularity!r}")
        if end <= start:
            raise InvalidUsageQuery("end must be after start")
        buckets = await usage_repository.timeseries(
            team_id,
            start=start,
            end=end,
            granularity=granularity,  # type: ignore[arg-type]  # validated above
            model_name=model,
            requested_alias=alias,
            api_key_id=api_key_id,
        )
        series = UsageTimeseries(
            team_id=team_id,
            granularity=granularity,  # type: ignore[arg-type]  # validated above
            start=start,
            end=end,
            buckets=buckets,
        )
        return UsageTimeseriesResponse.from_timeseries(series)

    @get(
        "/{team_id:uuid}/cache/savings",
        summary="Response-cache hit rate and cost saved for the team",
    )
    async def team_cache_savings(
        self,
        team_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        usage_repository: NamedDependency[UsageRepository],
    ) -> dict[str, Any]:
        """Response-cache observability (Plan 04 Phase 3), mirroring the
        smart-routing savings endpoint: hit rate + Σ(avoided cost) across the
        team's own usage history."""
        await team_service.ensure_team_permission(current_user, team_id, Permission.USAGE_READ)
        avoided_cost, priced_hits, hits_without_price, total = await usage_repository.cache_savings(
            team_id
        )
        return {
            "team_id": str(team_id),
            **_cache_savings_response(avoided_cost, priced_hits, hits_without_price, total),
        }


# Platform-wide cache savings aggregate — outside the /teams controller because
# it spans every team (platform-admin only), mirroring `platform_routing_savings`
# in `web/routing/controller.py`.
@get(
    "/cache/savings",
    summary="Response-cache hit rate and cost saved across the whole platform",
    dependencies={"admin_user": Provide(provide_current_admin)},
    tags=["teams"],
)
async def platform_cache_savings(
    admin_user: NamedDependency[User],
    usage_repository: NamedDependency[UsageRepository],
) -> dict[str, Any]:
    (
        avoided_cost,
        priced_hits,
        hits_without_price,
        total,
    ) = await usage_repository.platform_cache_savings()
    return _cache_savings_response(avoided_cost, priced_hits, hits_without_price, total)

"""Team-scoped CRUD for smart routers (virtual models).

Routers are managed with the same permission as models (`models:manage`) —
a router is a virtual model. Authorization goes through the central
`TeamService.ensure_team_permission` gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from litestar import Controller, Request, Response, delete, get, post, put
from litestar.di import NamedDependency, Provide
from litestar.params import FromPath, FromQuery

from litestar_gateway.application.routing.service import RouterService
from litestar_gateway.application.team_service import TeamService
from litestar_gateway.domain.authorization import Permission
from litestar_gateway.domain.entities import User
from litestar_gateway.domain.exceptions import InvalidRouterConfig
from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.domain.routing import CandidateModel, QualityTier, RouterConfig
from litestar_gateway.infrastructure.web.audit.recorder import record_audit
from litestar_gateway.infrastructure.web.session.dependencies import provide_current_user


@dataclass(frozen=True)
class CandidateRequest:
    model_name: str
    description: str
    quality_tier: str
    supports_vision: bool = False
    supports_tools: bool = False
    supports_json_schema: bool = False
    context_window_tokens: int | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    # Relative traffic share for the "weighted" strategy (§ weighted routing).
    weight: float | None = None

    def to_entity(self) -> CandidateModel:
        try:
            tier = QualityTier(self.quality_tier.upper())
        except ValueError:
            valid = ", ".join(t.value for t in QualityTier)
            raise InvalidRouterConfig(f"quality_tier must be one of: {valid}") from None
        return CandidateModel(
            model_name=self.model_name,
            description=self.description,
            quality_tier=tier,
            supports_vision=self.supports_vision,
            supports_tools=self.supports_tools,
            supports_json_schema=self.supports_json_schema,
            context_window_tokens=self.context_window_tokens,
            input_cost_per_token=self.input_cost_per_token,
            output_cost_per_token=self.output_cost_per_token,
            weight=self.weight,
        )


@dataclass(frozen=True)
class RouterRequest:
    name: str
    candidates: list[CandidateRequest]
    default_model: str
    strategy: str = "complexity"
    strategy_config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    # Shadow strategy (§6): runs fire-and-forget after the active one; its
    # config lives under strategy_config["shadow"].
    shadow_strategy: str | None = None

    def to_entity(self, team_id: UUID, router_id: UUID | None = None) -> RouterConfig:
        return RouterConfig(
            id=router_id or uuid4(),
            team_id=team_id,
            name=self.name,
            candidates=tuple(candidate.to_entity() for candidate in self.candidates),
            default_model=self.default_model,
            strategy=self.strategy,
            strategy_config=self.strategy_config,
            enabled=self.enabled,
            created_at=datetime.now(UTC),
            shadow_strategy=self.shadow_strategy,
        )


@dataclass(frozen=True)
class RouterResponse:
    id: UUID
    name: str
    candidates: list[dict[str, Any]]
    default_model: str
    strategy: str
    strategy_config: dict[str, Any]
    enabled: bool
    created_at: datetime
    shadow_strategy: str | None

    @classmethod
    def from_entity(cls, router: RouterConfig) -> RouterResponse:
        import dataclasses

        return cls(
            id=router.id,
            name=router.name,
            candidates=[dataclasses.asdict(candidate) for candidate in router.candidates],
            default_model=router.default_model,
            strategy=router.strategy,
            strategy_config=router.strategy_config,
            enabled=router.enabled,
            created_at=router.created_at,
            shadow_strategy=router.shadow_strategy,
        )


@dataclass(frozen=True)
class DecisionResponse:
    id: UUID
    strategy: str
    chosen_model: str
    tier: str | None
    score: float | None
    signals: list[str]
    decision_ms: float
    is_shadow: bool
    fallback_used: bool
    prompt_tokens: int | None
    completion_tokens: int | None
    created_at: datetime

    @classmethod
    def from_record(cls, r) -> DecisionResponse:
        return cls(
            id=r.id,
            strategy=r.strategy,
            chosen_model=r.chosen_model,
            tier=r.tier,
            score=r.score,
            signals=list(r.signals),
            decision_ms=r.decision_ms,
            is_shadow=r.is_shadow,
            fallback_used=r.fallback_used,
            prompt_tokens=r.prompt_tokens,
            completion_tokens=r.completion_tokens,
            created_at=r.created_at,
        )


class RouterController(Controller):
    path = "/teams"
    tags = ["routers"]
    dependencies = {"current_user": Provide(provide_current_user)}

    @post("/{team_id:uuid}/routers", summary="Create a smart router (virtual model)")
    async def create_router(
        self,
        request: Request,
        team_id: FromPath[UUID],
        data: RouterRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        router_service: NamedDependency[RouterService],
        audit_log: NamedDependency[AuditLog],
    ) -> RouterResponse:
        await team_service.ensure_team_permission(current_user, team_id, Permission.MODELS_MANAGE)
        router = await router_service.create(data.to_entity(team_id))
        await record_audit(
            audit_log,
            request,
            current_user,
            "router.create",
            target_type="router",
            target_id=router.id,
            detail=router.name,
        )
        return RouterResponse.from_entity(router)

    @get("/{team_id:uuid}/routers", summary="List the team's routers")
    async def list_routers(
        self,
        team_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        router_service: NamedDependency[RouterService],
    ) -> list[RouterResponse]:
        await team_service.ensure_team_permission(current_user, team_id, Permission.MODELS_READ)
        routers = await router_service.list_by_team(team_id)
        return [RouterResponse.from_entity(router) for router in routers]

    @put("/{team_id:uuid}/routers/{router_id:uuid}", summary="Replace a router definition")
    async def update_router(
        self,
        request: Request,
        team_id: FromPath[UUID],
        router_id: FromPath[UUID],
        data: RouterRequest,
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        router_service: NamedDependency[RouterService],
        audit_log: NamedDependency[AuditLog],
    ) -> RouterResponse:
        await team_service.ensure_team_permission(current_user, team_id, Permission.MODELS_MANAGE)
        await router_service.get(team_id, router_id)  # 404 before validation
        router = await router_service.update(data.to_entity(team_id, router_id))
        await record_audit(
            audit_log,
            request,
            current_user,
            "router.update",
            target_type="router",
            target_id=router.id,
            detail=router.name,
        )
        return RouterResponse.from_entity(router)

    @delete("/{team_id:uuid}/routers/{router_id:uuid}", summary="Delete a router")
    async def delete_router(
        self,
        request: Request,
        team_id: FromPath[UUID],
        router_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        router_service: NamedDependency[RouterService],
        audit_log: NamedDependency[AuditLog],
    ) -> None:
        await team_service.ensure_team_permission(current_user, team_id, Permission.MODELS_MANAGE)
        await router_service.delete(team_id, router_id)
        await record_audit(
            audit_log,
            request,
            current_user,
            "router.delete",
            target_type="router",
            target_id=router_id,
        )

    @get(
        "/{team_id:uuid}/routers/{router_id:uuid}/decisions",
        summary="Routing decisions (most recent first)",
    )
    async def list_decisions(
        self,
        team_id: FromPath[UUID],
        router_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        router_service: NamedDependency[RouterService],
        strategy: FromQuery[str | None] = None,
        model: FromQuery[str | None] = None,
        shadow: FromQuery[bool | None] = None,
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> list[DecisionResponse]:
        await team_service.ensure_team_permission(current_user, team_id, Permission.USAGE_READ)
        from litestar_gateway.domain.pagination import resolve_page

        page_limit, page_offset = resolve_page(limit, offset)
        records = await router_service.list_decisions(
            team_id,
            router_id,
            strategy=strategy,
            chosen_model=model,
            is_shadow=shadow,
            limit=page_limit,
            offset=page_offset,
        )
        return [DecisionResponse.from_record(r) for r in records]

    @get(
        "/{team_id:uuid}/routers/{router_id:uuid}/stats",
        summary="Request distribution per chosen model / tier",
    )
    async def router_stats(
        self,
        team_id: FromPath[UUID],
        router_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        router_service: NamedDependency[RouterService],
    ) -> dict[str, Any]:
        await team_service.ensure_team_permission(current_user, team_id, Permission.USAGE_READ)
        return await router_service.stats(team_id, router_id)

    @get(
        "/{team_id:uuid}/routers/{router_id:uuid}/savings",
        summary="Estimated savings vs the most expensive capable candidate",
    )
    async def router_savings(
        self,
        team_id: FromPath[UUID],
        router_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        router_service: NamedDependency[RouterService],
    ) -> dict[str, Any]:
        await team_service.ensure_team_permission(current_user, team_id, Permission.USAGE_READ)
        return await router_service.savings(team_id, router_id)

    @get(
        "/{team_id:uuid}/routers/{router_id:uuid}/decisions/export",
        summary="JSONL export of judge/escalation decisions (distillation dataset, §S6)",
    )
    async def export_decisions(
        self,
        team_id: FromPath[UUID],
        router_id: FromPath[UUID],
        current_user: NamedDependency[User],
        team_service: NamedDependency[TeamService],
        router_service: NamedDependency[RouterService],
        strategy: FromQuery[str | None] = None,
        limit: FromQuery[int | None] = None,
        offset: FromQuery[int | None] = None,
    ) -> Response:
        """One JSON object per line: {text, system_prompt, chosen_model,
        strategy, score, signals, timestamp}. Only decisions that stored their
        text (judge decisions and hybrid escalations) are exported — the
        intended path is judge → dataset → small local classifier.

        Gated on `decisions:read` (not `usage:read`): the export carries raw
        end-user prompts, which billing-oriented roles must never see."""
        await team_service.ensure_team_permission(current_user, team_id, Permission.DECISIONS_READ)
        from litestar_gateway.domain.pagination import resolve_page

        page_limit, page_offset = resolve_page(limit, offset)
        records = await router_service.list_decisions(
            team_id, router_id, strategy=strategy, limit=page_limit, offset=page_offset
        )
        lines = [
            json.dumps(
                {
                    "text": r.user_text,
                    "system_prompt": r.system_prompt,
                    "chosen_model": r.chosen_model,
                    "strategy": r.strategy,
                    "score": r.score,
                    "signals": list(r.signals),
                    "timestamp": r.created_at.isoformat(),
                },
                ensure_ascii=False,
            )
            for r in records
            if r.user_text is not None
        ]
        body = "\n".join(lines) + ("\n" if lines else "")
        return Response(body, media_type="application/x-ndjson")

"""SQLAlchemy adapter for guardrail rules.

The webhook signing secret is envelope-encrypted with the keyring, the same
scheme as provider credentials and the SSO client secret. Only `resolve` — the
call path — decrypts; every management read returns the entity with
`has_secret` and nothing more, so a controller cannot leak what it never holds.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import ActiveGuardrailRule, GuardrailRule, resolve_chain
from litestar_gateway.domain.exceptions import CredentialMisconfigured, SaltKeyMissing
from litestar_gateway.domain.guardrails import Direction
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.orm import GuardrailRuleModel

# The JSON key inside the encrypted blob, not a secret itself.
_SECRET_FIELD = "signing_secret"  # pragma: allowlist secret


class SQLAlchemyGuardrailRuleRepository:
    def __init__(self, session: AsyncSession, keyring: Keyring | None = None) -> None:
        # `keyring` is needed only to store or read a secret; listing rules
        # (metadata) works without one.
        self._session = session
        self._keyring = keyring

    def _require_keyring(self) -> Keyring:
        if self._keyring is None:
            raise SaltKeyMissing("SALT_KEY is not configured")
        return self._keyring

    async def list_for_team(self, team_id: UUID) -> list[GuardrailRule]:
        rows = await self._session.scalars(
            select(GuardrailRuleModel)
            .where(GuardrailRuleModel.team_id == team_id)
            .order_by(GuardrailRuleModel.position, GuardrailRuleModel.name)
        )
        return [row.to_entity() for row in rows]

    async def get(self, team_id: UUID, rule_id: UUID) -> GuardrailRule | None:
        row = await self._row(team_id, rule_id)
        return row.to_entity() if row else None

    async def add(self, rule: GuardrailRule, *, secret: str | None = None) -> GuardrailRule:
        row = GuardrailRuleModel(
            id=rule.id,
            team_id=rule.team_id,
            model_id=rule.model_id,
            name=rule.name,
            kind=rule.kind.value,
            direction=rule.direction.value,
            position=rule.position,
            fail_policy=rule.fail_policy.value,
            enabled=rule.enabled,
            config=dict(rule.config),
        )
        await self._apply_secret(row, secret)
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row.to_entity()

    async def update(self, rule: GuardrailRule, *, secret: str | None = None) -> GuardrailRule:
        row = await self._row(rule.team_id, rule.id)
        if row is None:
            raise LookupError(str(rule.id))
        row.model_id = rule.model_id
        row.name = rule.name
        row.kind = rule.kind.value
        row.direction = rule.direction.value
        row.position = rule.position
        row.fail_policy = rule.fail_policy.value
        row.enabled = rule.enabled
        row.config = dict(rule.config)
        # `secret is None` keeps the stored one: it is never readable, so an
        # operator editing a timeout cannot resubmit it, and treating omission as
        # "clear" would silently unsign the endpoint.
        await self._apply_secret(row, secret)
        await self._session.commit()
        await self._session.refresh(row)
        return row.to_entity()

    async def remove(self, team_id: UUID, rule_id: UUID) -> bool:
        # Any: the async execute() is typed Result, but at runtime it is a
        # CursorResult exposing rowcount.
        result: Any = await self._session.execute(
            delete(GuardrailRuleModel).where(
                GuardrailRuleModel.team_id == team_id, GuardrailRuleModel.id == rule_id
            )
        )
        await self._session.commit()
        return bool(result.rowcount)

    async def resolve(
        self, team_id: UUID, model_id: UUID, direction: Direction
    ) -> list[ActiveGuardrailRule]:
        rows = await self._session.scalars(
            select(GuardrailRuleModel).where(
                GuardrailRuleModel.team_id == team_id,
                GuardrailRuleModel.enabled.is_(True),
                GuardrailRuleModel.direction == direction.value,
            )
        )
        by_id = {row.id: row for row in rows}
        chain = resolve_chain(
            [row.to_entity() for row in by_id.values()], model_id=model_id, direction=direction
        )
        return [
            ActiveGuardrailRule(rule=rule, secret=await self._secret(by_id[rule.id]))
            for rule in chain
        ]

    async def _row(self, team_id: UUID, rule_id: UUID) -> GuardrailRuleModel | None:
        return await self._session.scalar(
            select(GuardrailRuleModel).where(
                GuardrailRuleModel.team_id == team_id, GuardrailRuleModel.id == rule_id
            )
        )

    async def _apply_secret(self, row: GuardrailRuleModel, secret: str | None) -> None:
        if secret is None:
            return
        key_id, cipher = await self._require_keyring().active_credential_cipher()
        row.encrypted_secret = cipher.encrypt({_SECRET_FIELD: secret})
        row.key_id = key_id

    async def _secret(self, row: GuardrailRuleModel) -> str | None:
        if row.encrypted_secret is None or row.key_id is None:
            return None
        cipher = await self._require_keyring().credential_cipher_for(row.key_id)
        if cipher is None:  # pragma: no cover - a missing key row is not expected
            raise CredentialMisconfigured(f"encryption key for guardrail rule {row.id} is missing")
        return cipher.decrypt(row.encrypted_secret)[_SECRET_FIELD]

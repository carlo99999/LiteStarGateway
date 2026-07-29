"""SQLAlchemy adapter implementing the `BudgetRepository` port.

The per-team webhook signing secret is envelope-encrypted with the keyring, the
same scheme as provider credentials. Only `alert_webhook_secret` decrypts it —
`get` returns `has_alert_webhook_secret` and nothing more, so no read path that
serves a console can leak it.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.entities import Budget
from litestar_gateway.domain.exceptions import CredentialMisconfigured, SaltKeyMissing
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.orm import TeamBudgetModel

# The JSON key inside the encrypted blob, not a secret itself.
_SECRET_FIELD = "alert_webhook_secret"  # pragma: allowlist secret


class SQLAlchemyBudgetRepository:
    def __init__(self, session: AsyncSession, keyring: Keyring | None = None) -> None:
        # `keyring` is needed only for the per-team webhook secret; the budget
        # itself (the hot-path read for the gate) works without one.
        self._session = session
        self._keyring = keyring

    def _require_keyring(self) -> Keyring:
        if self._keyring is None:
            raise SaltKeyMissing("SALT_KEY is not configured")
        return self._keyring

    async def get(self, team_id: UUID) -> Budget | None:
        row = await self._session.scalar(
            select(TeamBudgetModel).where(TeamBudgetModel.team_id == team_id)
        )
        return row.to_entity() if row else None

    async def alert_webhook_secret(self, team_id: UUID) -> str | None:
        row = await self._session.scalar(
            select(TeamBudgetModel).where(TeamBudgetModel.team_id == team_id)
        )
        if row is None or row.encrypted_alert_webhook_secret is None:
            return None
        if row.alert_webhook_secret_key_id is None:  # pragma: no cover - written together
            return None
        cipher = await self._require_keyring().credential_cipher_for(
            row.alert_webhook_secret_key_id
        )
        if cipher is None:  # pragma: no cover - a missing key row is not expected
            raise CredentialMisconfigured(f"encryption key for team {team_id} alert is missing")
        return cipher.decrypt(row.encrypted_alert_webhook_secret)[_SECRET_FIELD]

    async def set(self, budget: Budget, *, alert_webhook_secret: str | None = None) -> Budget:
        try:
            return await self._upsert(budget, alert_webhook_secret)
        except IntegrityError:
            # Concurrent insert for the same team lost the unique-constraint
            # race; retry once — the row now exists, so this becomes an update.
            await self._session.rollback()
            return await self._upsert(budget, alert_webhook_secret)

    async def _upsert(self, budget: Budget, alert_webhook_secret: str | None = None) -> Budget:
        row = await self._session.scalar(
            select(TeamBudgetModel).where(TeamBudgetModel.team_id == budget.team_id)
        )
        if row is None:
            row = TeamBudgetModel(
                id=budget.id,
                team_id=budget.team_id,
                limit_cost=budget.limit_cost,
                window=budget.window.value,
                thresholds=list(budget.thresholds),
                alert_webhook_url=budget.alert_webhook_url,
                alert_email=budget.alert_email,
            )
            self._session.add(row)
        else:
            row.limit_cost = budget.limit_cost
            row.window = budget.window.value
            row.thresholds = list(budget.thresholds)
            row.alert_webhook_url = budget.alert_webhook_url
            row.alert_email = budget.alert_email
        if alert_webhook_secret is not None:
            key_id, cipher = await self._require_keyring().active_credential_cipher()
            row.encrypted_alert_webhook_secret = cipher.encrypt(
                {_SECRET_FIELD: alert_webhook_secret}
            )
            row.alert_webhook_secret_key_id = key_id
        # else: keep whatever is stored — omission means unchanged, never clear.
        await self._session.commit()
        await self._session.refresh(row)
        return row.to_entity()

    async def remove(self, team_id: UUID) -> None:
        await self._session.execute(
            delete(TeamBudgetModel).where(TeamBudgetModel.team_id == team_id)
        )
        await self._session.commit()

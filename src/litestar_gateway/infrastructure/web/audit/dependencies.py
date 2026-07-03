"""Dependency wiring: build the AuditLog adapter from a DB session."""

from __future__ import annotations

from litestar.di import NamedDependency
from sqlalchemy.ext.asyncio import AsyncSession

from litestar_gateway.domain.ports import AuditLog
from litestar_gateway.infrastructure.persistence.audit_repository import SQLAlchemyAuditLog


def provide_audit_log(db_session: NamedDependency[AsyncSession]) -> AuditLog:
    return SQLAlchemyAuditLog(db_session)

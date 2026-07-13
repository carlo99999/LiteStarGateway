"""Per-team request-rate limiting (RPM) on the inference path.

A team with `rate_limit_rpm` set rejects requests past the limit within the
window with 429 + Retry-After; a team without one is unlimited.
"""

from __future__ import annotations

import pytest
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_429_TOO_MANY_REQUESTS,
)
from litestar.testing import AsyncTestClient

from .conftest import _bearer, _patch, _setup_team

_CHAT = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


async def _set_team_rpm(client: AsyncTestClient, admin: str, team_id: str, rpm: int) -> None:
    # PATCH requires the name; the harness creates the team as "Core".
    resp = await client.patch(
        f"/teams/{team_id}",
        json={"name": "Core", "rate_limit_rpm": rpm},
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_200_OK, resp.text
    assert resp.json()["rate_limit_rpm"] == rpm


async def test_team_rpm_limit_blocks_after_limit(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    key, team_id, admin = await _setup_team(client)
    await _set_team_rpm(client, admin, team_id, 1)

    first = await client.post("/v1/chat/completions", json=_CHAT, headers=_bearer(key))
    assert first.status_code == HTTP_200_OK, first.text

    second = await client.post("/v1/chat/completions", json=_CHAT, headers=_bearer(key))
    assert second.status_code == HTTP_429_TOO_MANY_REQUESTS, second.text
    assert "retry-after" in {k.lower() for k in second.headers}


async def test_no_limit_is_unlimited(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A team without rate_limit_rpm is never throttled.
    _patch(monkeypatch)
    key, _team_id, _admin = await _setup_team(client)
    for _ in range(5):
        resp = await client.post("/v1/chat/completions", json=_CHAT, headers=_bearer(key))
        assert resp.status_code == HTTP_200_OK, resp.text

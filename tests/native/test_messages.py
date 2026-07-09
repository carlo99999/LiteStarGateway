"""Phase 0 guard tests for the Anthropic-native `POST /v1/messages` endpoint.

The endpoint has no provider logic yet: it exists to prove the surface is fully
guarded (auth, per-IP rate limit, model resolution + enable/type/credential
checks) and bills nothing. A request that clears every guard reaches the stub
and gets a 501, mirroring how the gateway signals every other capability gap.
"""

from __future__ import annotations

from completions.conftest import ANTHROPIC_VALUES, _bearer, _setup_team, _team_usage
from litestar.status_codes import (
    HTTP_401_UNAUTHORIZED,
    HTTP_404_NOT_FOUND,
    HTTP_409_CONFLICT,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_501_NOT_IMPLEMENTED,
)
from litestar.testing import AsyncTestClient

from litestar_gateway.infrastructure.web.rate_limit import INFERENCE_RATE_LIMIT

_BODY = {"model": "m", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}


async def test_unauthenticated_401(client: AsyncTestClient) -> None:
    # No bearer token: the shared API-key middleware rejects before any handler.
    resp = await client.post("/v1/messages", json=_BODY)
    assert resp.status_code == HTTP_401_UNAUTHORIZED


async def test_over_rate_limit_429(client: AsyncTestClient) -> None:
    # The per-IP inference limiter runs *before* auth (same middleware the
    # OpenAI endpoints use), so unauthenticated floods are throttled: the budget
    # of requests is let through to the 401, and the next one is blocked.
    limit = INFERENCE_RATE_LIMIT[1]
    for _ in range(limit):
        resp = await client.post("/v1/messages", json=_BODY)
        assert resp.status_code == HTTP_401_UNAUTHORIZED
    blocked = await client.post("/v1/messages", json=_BODY)
    assert blocked.status_code == HTTP_429_TOO_MANY_REQUESTS


async def test_unknown_model_404(client: AsyncTestClient) -> None:
    key, _, _ = await _setup_team(
        client, provider="anthropic", values=ANTHROPIC_VALUES, model_type="chat"
    )
    resp = await client.post("/v1/messages", json={**_BODY, "model": "nope"}, headers=_bearer(key))
    assert resp.status_code == HTTP_404_NOT_FOUND


async def test_disabled_model_409(client: AsyncTestClient) -> None:
    key, _, _ = await _setup_team(
        client, provider="anthropic", values=ANTHROPIC_VALUES, model_type="chat", enabled=False
    )
    resp = await client.post("/v1/messages", json=_BODY, headers=_bearer(key))
    assert resp.status_code == HTTP_409_CONFLICT


async def test_valid_model_reaches_stub_501_and_bills_nothing(client: AsyncTestClient) -> None:
    # A fully valid, authed request clears every guard and reaches the not-yet
    # implemented provider dispatch -> 501. Because no dispatch (and no metering)
    # runs, the team's usage ledger stays empty: the surface bills nothing.
    key, team, admin = await _setup_team(
        client, provider="anthropic", values=ANTHROPIC_VALUES, model_type="chat"
    )
    resp = await client.post("/v1/messages", json=_BODY, headers=_bearer(key))
    assert resp.status_code == HTTP_501_NOT_IMPLEMENTED
    assert await _team_usage(client, team, admin) == []

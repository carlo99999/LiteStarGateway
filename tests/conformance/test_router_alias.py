"""Contract: a router (virtual model) alias is usable as an OpenAI `model` name.

A framework configured with `model="auto"` calls the gateway exactly like any
other model; the gateway resolves the router, dispatches to a candidate, and
bills the call. Driven with the official SDK; routing/billing internals are
covered in tests/routing — here we assert the alias works over the wire."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from completions.conftest import _bearer, _setup_team
from litestar.testing import AsyncTestClient
from openai import AsyncOpenAI

from .conftest import FakeUpstream, _patch_upstream


async def _make_router(client: AsyncTestClient, admin: str, team: str) -> None:
    # `m` already exists (from _setup_team). A one-candidate complexity router
    # named "auto" is enough to prove alias resolution over the wire.
    resp = await client.post(
        f"/teams/{team}/routers",
        json={
            "name": "auto",
            "default_model": "m",
            "strategy": "complexity",
            "candidates": [{"model_name": "m", "description": "general", "quality_tier": "SIMPLE"}],
        },
        headers=_bearer(admin),
    )
    assert resp.status_code in (200, 201), resp.text


async def test_router_alias_is_callable_as_a_model(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_upstream(monkeypatch)
    api_key, team, admin = await _setup_team(client)
    await _make_router(client, admin, team)

    completion = await sdk(api_key).chat.completions.create(
        model="auto",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert completion.choices[0].message.content is not None
    # The router dispatched to a real candidate model upstream, not "auto".
    assert FakeUpstream.last_kwargs["model"] == "gpt-4o"


async def test_router_alias_meters_usage(
    client: AsyncTestClient,
    sdk: Callable[[str], AsyncOpenAI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_upstream(monkeypatch)
    api_key, team, admin = await _setup_team(client)
    await _make_router(client, admin, team)
    await sdk(api_key).chat.completions.create(
        model="auto", messages=[{"role": "user", "content": "hello"}]
    )
    # The call is billed to the team like any inference call.
    usage = (await client.get(f"/teams/{team}/usage", headers=_bearer(admin))).json()
    assert sum(row["calls"] for row in usage) >= 1

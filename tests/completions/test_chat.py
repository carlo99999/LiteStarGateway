"""Basic chat completions: param sanitizing/enforcement, usage, key spending."""

from __future__ import annotations

from pathlib import Path

import pytest
from litestar.status_codes import HTTP_200_OK
from litestar.testing import AsyncTestClient

from litestar_gateway.config import DEFAULT_MAX_RETRIES, DEFAULT_REQUEST_TIMEOUT
from litestar_gateway.domain.request_policy import MAX_N

from .conftest import ADMIN_EMAIL, OPENAI_VALUES, FakeClient, _admin, _bearer, _patch, _setup


async def test_openai_chat_completions(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeClient.last_kwargs["model"] == "gpt-4o"  # alias -> upstream id
    assert FakeClient.last_init["api_key"] == "sk-x"
    assert resp.json()["model"] == "gpt-4o"


async def test_request_params_are_sanitized_before_provider(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "n": 999,  # cost driver → clamped
            "extra_headers": {"X-Evil": "1"},  # transport injection → dropped
            "extra_body": {"foo": "bar"},  # → dropped
        },
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert "extra_headers" not in FakeClient.last_kwargs
    assert "extra_body" not in FakeClient.last_kwargs
    assert FakeClient.last_kwargs["n"] == MAX_N


async def test_enforced_params_cannot_be_overridden_by_client(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H15: params_enforced is admin policy applied last — the client cannot win.
    _patch(monkeypatch)
    api_key = await _setup(client, params_enforced={"response_format": {"type": "json_object"}})
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "text"},  # attempt to override admin policy
        },
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK
    assert FakeClient.last_kwargs["response_format"] == {"type": "json_object"}


async def test_default_params_remain_overridable_by_client(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H15: plain `params` are defaults — the client's value still wins.
    _patch(monkeypatch)
    api_key = await _setup(client, params={"temperature": 0.2})
    await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.9},
        headers=_bearer(api_key),
    )
    assert FakeClient.last_kwargs["temperature"] == 0.9


async def test_max_output_tokens_clamps_client_value(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H15: the per-model ceiling lowers an over-large client request (min clamp).
    _patch(monkeypatch)
    api_key = await _setup(client, max_output_tokens=256)
    await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 100_000},
        headers=_bearer(api_key),
    )
    assert FakeClient.last_kwargs["max_tokens"] == 256


async def test_max_output_tokens_injected_when_client_omits(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # H15: omitting max_tokens must not bypass the cap — inject it at the ceiling.
    _patch(monkeypatch)
    api_key = await _setup(client, max_output_tokens=256)
    await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(api_key),
    )
    assert FakeClient.last_kwargs["max_tokens"] == 256


async def test_provider_client_gets_timeout_and_retries(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    api_key = await _setup(client)
    await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(api_key),
    )
    # The SDK client is built with a bounded timeout + retry budget (no 10-min hang).
    assert FakeClient.last_init["timeout"] == DEFAULT_REQUEST_TIMEOUT
    assert FakeClient.last_init["max_retries"] == DEFAULT_MAX_RETRIES


async def test_usage_is_recorded_and_queryable(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    admin = await _admin(client)
    cred = (
        await client.post(
            "/credentials",
            json={"name": "c", "provider": "openai", "values": OPENAI_VALUES},
            headers=_bearer(admin),
        )
    ).json()["id"]
    org = (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(admin))
    ).json()["id"]
    team = (
        await client.post(
            f"/organizations/{org}/teams",
            json={"name": "Core", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    await client.post(
        f"/teams/{team}/models",
        json={
            "name": "m",
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o",
            "enabled": True,
        },
        headers=_bearer(admin),
    )
    key = (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()["plaintext"]

    # Two chat calls → usage recorded (the fake reports 1 token in + 1 out each).
    for _ in range(2):
        await client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            headers=_bearer(key),
        )

    usage = await client.get(f"/teams/{team}/usage", headers=_bearer(admin))
    assert usage.status_code == HTTP_200_OK
    rows = usage.json()
    assert len(rows) == 1
    assert rows[0]["model"] == "m"
    assert rows[0]["prompt_tokens"] == 2
    assert rows[0]["completion_tokens"] == 2
    assert rows[0]["total_tokens"] == 4
    assert rows[0]["calls"] == 2

    # Filtering by an unknown model returns nothing.
    filtered = await client.get(f"/teams/{team}/usage?model=nope", headers=_bearer(admin))
    assert filtered.json() == []


async def test_usage_record_failure_dead_letters_to_outbox(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # If the ledger write fails, the request still succeeds (fail-safe) and the
    # event is dead-lettered to the durable outbox — recoverable, not dropped.
    import sqlite3

    _patch(monkeypatch)
    from litestar_gateway.infrastructure.persistence import usage_repository

    async def boom(self: object, event: object) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(usage_repository.SQLAlchemyUsageRepository, "record", boom)
    api_key = await _setup(client)
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(api_key),
    )
    assert resp.status_code == HTTP_200_OK  # request unaffected by the billing failure

    # The event landed in the outbox (a separate connection sees the committed row).
    conn = sqlite3.connect(str(tmp_path / "inf.db"))
    try:
        (pending,) = conn.execute("SELECT count(*) FROM pending_usage_event").fetchone()
    finally:
        conn.close()
    assert pending == 1


async def test_key_spending_report_includes_revoked_keys(
    client: AsyncTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Past (revoked) keys and their accumulated spend must stay visible.
    _patch(monkeypatch)
    admin = await _admin(client)
    cred = (
        await client.post(
            "/credentials",
            json={"name": "c", "provider": "openai", "values": OPENAI_VALUES},
            headers=_bearer(admin),
        )
    ).json()["id"]
    org = (
        await client.post("/organizations", json={"name": "Acme"}, headers=_bearer(admin))
    ).json()["id"]
    team = (
        await client.post(
            f"/organizations/{org}/teams",
            json={"name": "Core", "admin_email": ADMIN_EMAIL},
            headers=_bearer(admin),
        )
    ).json()["id"]
    await client.post(
        f"/teams/{team}/models",
        json={
            "name": "m",
            "provider": "openai",
            "credential_id": cred,
            "type": "chat",
            "provider_model_id": "gpt-4o",
            "enabled": True,
        },
        headers=_bearer(admin),
    )
    created = (
        await client.post(f"/teams/{team}/keys", json={"name": "k"}, headers=_bearer(admin))
    ).json()
    key_id, key = created["id"], created["plaintext"]

    # One chat call → the fake reports 1 prompt + 1 completion token.
    await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        headers=_bearer(key),
    )

    spending = (await client.get(f"/teams/{team}/keys/spending", headers=_bearer(admin))).json()
    assert len(spending) == 1
    assert spending[0]["id"] == key_id
    assert spending[0]["is_active"] is True
    assert spending[0]["prompt_tokens"] == 1
    assert spending[0]["completion_tokens"] == 1
    assert spending[0]["calls"] == 1

    # After revoking, the key + its spend are still reported (now inactive).
    await client.delete(f"/teams/{team}/keys/{key_id}", headers=_bearer(admin))
    revoked = (await client.get(f"/teams/{team}/keys/spending", headers=_bearer(admin))).json()
    assert len(revoked) == 1
    assert revoked[0]["id"] == key_id
    assert revoked[0]["is_active"] is False
    assert revoked[0]["calls"] == 1

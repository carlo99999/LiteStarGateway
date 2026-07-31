"""Creation-time validation of credential `values` per provider."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from litestar.status_codes import HTTP_201_CREATED, HTTP_400_BAD_REQUEST
from litestar.testing import AsyncTestClient

from litestar_gateway.app import create_app
from litestar_gateway.config import Settings

MASTER_KEY = "master-secret"  # pragma: allowlist secret
ADMIN_EMAIL = "admin@example.com"
JWT_SECRET = "test-secret-key-0123456789-abcdefghij"  # pragma: allowlist secret
SALT_KEY = "unit-test-salt-key"  # pragma: allowlist secret


@pytest.fixture
async def client(database_url: str) -> AsyncIterator[AsyncTestClient]:
    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=SALT_KEY,
    )
    async with AsyncTestClient(app=create_app(settings)) as test_client:
        yield test_client


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin_token(client: AsyncTestClient) -> str:
    return (
        await client.post("/login", json={"email": ADMIN_EMAIL, "password": MASTER_KEY})
    ).json()["access_token"]


async def _create(client: AsyncTestClient, name: str, provider: str, values: dict[str, str]) -> int:
    admin = await _admin_token(client)
    resp = await client.post(
        "/credentials",
        json={"name": name, "provider": provider, "values": values},
        headers=_bearer(admin),
    )
    return resp.status_code


VALID = [
    ("openai", {"api_key": "sk-x"}),  # pragma: allowlist secret
    (
        "openai",
        {
            "api_key": "sk-x",  # pragma: allowlist secret
            "api_base": "https://proxy",
            "organization": "org-1",
        },  # pragma: allowlist secret
    ),
    (
        "anthropic",
        {"api_key": "sk-ant-x", "api_base": "https://proxy"},  # pragma: allowlist secret
    ),
    (
        "azure_openai",
        {
            "api_key": "az",  # pragma: allowlist secret
            "api_base": "https://a.openai.azure.com",
            "api_version": "2024-02-15",
        },
    ),
    # vertex_credentials is optional: the adapter falls back to ADC.
    ("vertex_ai", {"vertex_project": "p", "vertex_location": "us-central1"}),
    (
        "vertex_ai",
        {"vertex_project": "p", "vertex_location": "us-central1", "vertex_credentials": "{}"},
    ),
    (
        "databricks",
        {"api_key": "dapi-x", "api_base": "https://w.databricks.com"},  # pragma: allowlist secret
    ),
    (
        "bedrock",
        {"region": "eu-west-1", "aws_access_key_id": "AKIA", "aws_secret_access_key": "s"},
    ),
    (
        "bedrock",
        {
            "region": "eu-west-1",
            "aws_access_key_id": "AKIA",
            "aws_secret_access_key": "s",
            "aws_session_token": "tok",
        },
    ),
]


@pytest.mark.parametrize(("provider", "values"), VALID)
async def test_valid_values_accepted(
    client: AsyncTestClient, provider: str, values: dict[str, str]
) -> None:
    assert await _create(client, f"ok-{provider}", provider, values) == HTTP_201_CREATED


INVALID = [
    ("openai", {}),  # missing api_key
    ("openai", {"api_base": "https://proxy"}),  # missing api_key
    ("openai", {"api_key": ""}),  # blank required value
    ("openai", {"api_key": "sk-x", "api_keys": "typo"}),  # pragma: allowlist secret
    (
        "anthropic",
        {"organization": "org-1", "api_key": "x"},  # pragma: allowlist secret
    ),
    (
        "azure_openai",
        {"api_key": "az", "api_base": "https://a"},  # pragma: allowlist secret
    ),
    ("vertex_ai", {"vertex_project": "p"}),  # missing vertex_location
    ("vertex_ai", {"api_key": "sk-x"}),  # pragma: allowlist secret
    ("databricks", {"api_key": "dapi-x"}),  # pragma: allowlist secret
    # missing region
    ("bedrock", {"aws_access_key_id": "AKIA", "aws_secret_access_key": "s"}),
    # litellm-style key the adapter does not read
    (
        "bedrock",
        {"aws_region_name": "us-east-1", "aws_access_key_id": "AKIA", "aws_secret_access_key": "s"},
    ),
]


@pytest.mark.parametrize(("provider", "values"), INVALID)
async def test_invalid_values_rejected_with_400(
    client: AsyncTestClient, provider: str, values: dict[str, str]
) -> None:
    assert await _create(client, f"bad-{provider}", provider, values) == HTTP_400_BAD_REQUEST


async def test_error_names_the_offending_keys(client: AsyncTestClient) -> None:
    admin = await _admin_token(client)
    resp = await client.post(
        "/credentials",
        json={
            "name": "bad",
            "provider": "azure_openai",
            "values": {"api_key": "az"},  # pragma: allowlist secret
        },
        headers=_bearer(admin),
    )
    assert resp.status_code == HTTP_400_BAD_REQUEST
    detail = resp.json()["detail"]
    assert "api_base" in detail
    assert "api_version" in detail


# ── ISSUE-053: a resolution failure is the operator's problem, not a 500 ──────


async def test_an_unresolvable_api_base_host_is_a_bad_request_not_a_server_error(
    database_url: str,
) -> None:
    """`getaddrinfo` raises `gaierror`, which subclasses **OSError**, not
    `ValueError` — so this path once escaped the service unmapped and became a 500.

    The host is authorized by *name* in the allowlist and simply does not resolve,
    which is an ordinary state: a self-hosted server that is down, a DNS record not
    yet propagated, a typo. Every other outcome of this validation is an actionable
    400, and this was the only one breaking that contract.

    `.invalid` is reserved by RFC 2606 and never resolves, so the failure is real
    rather than simulated by patching the resolver — which is the whole reason the
    defect survived a review round: it is only visible when the name genuinely does
    not resolve.
    """
    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret=JWT_SECRET,
        salt_key=SALT_KEY,
        # Authorized by name, so the allowlist is *not* what refuses it.
        openai_compatible_allowed_hosts=("vllm.unresolvable.invalid:8000",),
    )

    async with AsyncTestClient(app=create_app(settings)) as client:
        status = await _create(
            client,
            "self-hosted",
            "openai_compatible",
            {
                "api_key": "sk-local",  # pragma: allowlist secret
                "api_base": "https://vllm.unresolvable.invalid:8000/v1",
            },
        )

        assert status == HTTP_400_BAD_REQUEST

"""`MCP_ALLOWED_HOSTS` is optional, and what "optional" costs.

The default was fail-closed: an empty allowlist refused every target, so the
feature did nothing until an operator configured it. It is now **allowlist when
configured, SSRF deny-list when not** — public tool servers work out of the box,
and an entry is how an internal one gets authorized.

That is a deliberate loosening, so the tests here are mostly about what it did
*not* loosen:

- the cloud metadata endpoint is still refused with no configuration at all. If
  this file has one reason to exist, it is that line;
- a configured allowlist is still absolute — it refuses public hosts too, so the
  platform veto (D2) survives for operators who want it;
- `OPENAI_COMPATIBLE_ALLOWED_HOSTS` is **untouched** and still refuses everything
  when empty. The two share `EgressAllowlist`, and inverting the polarity in the
  shared guard rather than in a new function would have silently undone Round 15's
  ISSUE-034 fix on the provider path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from advanced_alchemy.extensions.litestar import base
from litestar.status_codes import HTTP_201_CREATED, HTTP_400_BAD_REQUEST
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from litestar_gateway.application.egress import (
    resolve_allowlisted_addresses,
    resolve_optionally_allowlisted_addresses,
)
from litestar_gateway.application.mcp_service import McpServerService
from litestar_gateway.config import Settings
from litestar_gateway.domain.egress_policy import EgressAllowlist, parse_allowlist
from litestar_gateway.domain.entities import Principal
from litestar_gateway.domain.exceptions import InvalidMcpServer, PermissionDenied
from litestar_gateway.infrastructure.keyring import Keyring
from litestar_gateway.infrastructure.persistence.mcp_repository import (
    SQLAlchemyMcpServerRepository,
)
from litestar_gateway.infrastructure.persistence.secret_key_repository import (
    SQLAlchemySecretKeyRepository,
)

TEAM = uuid4()
PRINCIPAL = Principal(user=None, api_key=None)

# What each name resolves to in these tests. The point of the split is that only
# the address class differs — nothing about the *name* tells the policy anything.
ADDRESSES = {
    "tools.example.com": ["93.184.216.34"],  # public unicast
    "tools.internal": ["10.9.0.7"],  # private
    "sneaky.example.com": ["169.254.169.254"],  # public name, metadata address
}

PUBLIC_URL = "https://tools.example.com/mcp"
INTERNAL_URL = "https://tools.internal:8443/mcp"


class FakeTeams:
    def __init__(self, *, allow: bool = True) -> None:
        self._allow = allow

    async def ensure_principal_team_permission(self, principal, team_id: UUID, permission):
        if not self._allow:
            raise PermissionDenied(str(permission))
        return None


@pytest.fixture(autouse=True)
def resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    import litestar_gateway.application.egress as egress_module

    async def resolve(host: str) -> list[str]:
        if host not in ADDRESSES:
            raise OSError(f"unknown host in this test: {host}")
        return ADDRESSES[host]

    monkeypatch.setattr(egress_module, "_resolve_host_addresses", resolve)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'egress.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(base.UUIDAuditBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as opened:
        yield opened
    await engine.dispose()


def _service(session: AsyncSession, *, allowlist: EgressAllowlist) -> McpServerService:
    keyring = Keyring(SQLAlchemySecretKeyRepository(session), "salt-key-material", "jwt-secret")
    return McpServerService(
        SQLAlchemyMcpServerRepository(session, keyring), FakeTeams(), allowlist=allowlist
    )


NO_ALLOWLIST = EgressAllowlist(entries=())


# ── with no allowlist configured ─────────────────────────────────────────────


async def test_a_public_tool_server_needs_no_configuration(session: AsyncSession) -> None:
    """The reason the default changed: a fresh deployment can use a hosted MCP
    server without an operator editing the environment first."""
    server = await _service(session, allowlist=NO_ALLOWLIST).create_server(
        PRINCIPAL, TEAM, name="hosted", url=PUBLIC_URL
    )

    assert server.url == PUBLIC_URL
    assert [
        s.name
        for s in await _service(session, allowlist=NO_ALLOWLIST).list_servers(PRINCIPAL, TEAM)
    ] == ["hosted"]


async def test_the_metadata_endpoint_is_refused_with_no_configuration(
    session: AsyncSession,
) -> None:
    """The line this whole module exists for.

    `169.254.169.254` returns cloud credentials to anything that asks. "Open by
    default" must not mean a team admin can point the gateway at it, so the
    deny-list is what an empty allowlist falls through to — not nothing.
    """
    service = _service(session, allowlist=NO_ALLOWLIST)

    with pytest.raises(InvalidMcpServer, match="not a public address"):
        await service.create_server(
            PRINCIPAL, TEAM, name="metadata", url="https://169.254.169.254/mcp"
        )
    # And by name, too: the policy looks at what the host resolves to, so a
    # respectable-looking name pointing at the metadata address is refused the
    # same way. A check on the *name* would miss this entirely.
    with pytest.raises(InvalidMcpServer, match="not a public address"):
        await service.create_server(
            PRINCIPAL, TEAM, name="sneaky", url="https://sneaky.example.com/mcp"
        )
    assert await service.list_servers(PRINCIPAL, TEAM) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/mcp",  # loopback
        "https://10.0.0.1/mcp",  # private
        "https://[::1]/mcp",  # loopback, v6
        INTERNAL_URL,  # a name that resolves privately
    ],
)
async def test_every_non_public_target_is_refused_with_no_configuration(
    session: AsyncSession, url: str
) -> None:
    with pytest.raises(InvalidMcpServer, match="not a public address"):
        await _service(session, allowlist=NO_ALLOWLIST).create_server(
            PRINCIPAL, TEAM, name="internal", url=url
        )


async def test_the_refusal_tells_the_operator_how_to_authorize_an_internal_host(
    session: AsyncSession,
) -> None:
    """A message that only says "not permitted" sends an operator looking for a
    permission problem. The fix for an internal tool server is an allowlist entry,
    and nothing else in the product hints at that."""
    with pytest.raises(InvalidMcpServer) as refused:
        await _service(session, allowlist=NO_ALLOWLIST).create_server(
            PRINCIPAL, TEAM, name="internal", url=INTERNAL_URL
        )

    assert "MCP_ALLOWED_HOSTS" in str(refused.value)
    # Not the provider's variable, which the shared resolver names in its own
    # message — that would send the operator to edit the wrong setting.
    assert "OPENAI_COMPATIBLE" not in str(refused.value)


# ── with an allowlist configured ─────────────────────────────────────────────


async def test_an_entry_authorizes_the_internal_host_the_deny_list_refused(
    session: AsyncSession,
) -> None:
    """The override, which is what the allowlist is *for*: reaching a private
    target is the one thing the deny-list would never permit on its own."""
    allowed = parse_allowlist(("tools.internal:8443",))

    server = await _service(session, allowlist=allowed).create_server(
        PRINCIPAL, TEAM, name="internal", url=INTERNAL_URL
    )

    assert server.url == INTERNAL_URL


async def test_a_configured_allowlist_refuses_public_hosts_too(session: AsyncSession) -> None:
    """The platform veto (D2) survives the loosened default: once an operator sets
    the list, it is exhaustive. Otherwise configuring it would only ever *add*
    reach, and an operator who wrote one entry to lock the gateway down would have
    achieved nothing."""
    service = _service(session, allowlist=parse_allowlist(("tools.internal:8443",)))

    with pytest.raises(InvalidMcpServer, match="not permitted by MCP_ALLOWED_HOSTS"):
        await service.create_server(PRINCIPAL, TEAM, name="hosted", url=PUBLIC_URL)


# ── what did not change ──────────────────────────────────────────────────────


async def test_the_provider_allowlist_still_refuses_everything_when_empty() -> None:
    """The regression that matters most here.

    `resolve_allowlisted_addresses` is shared with the openai_compatible provider,
    where an empty allowlist means the provider is unusable — and where clearing
    the list failing to stop existing credentials was ISSUE-034. The MCP change is
    a *new* function precisely so this one keeps refusing, and asserting it here is
    what stops somebody "simplifying" the two back together.
    """
    with pytest.raises(ValueError, match="no egress allowlist is configured"):
        await resolve_allowlisted_addresses("tools.example.com", 443, NO_ALLOWLIST)

    # ...while the MCP variant resolves the same public host happily.
    assert await resolve_optionally_allowlisted_addresses("tools.example.com", 443, NO_ALLOWLIST)


async def test_a_deployment_with_no_mcp_config_can_register_a_public_server(
    database_url: str,
) -> None:
    """The wired path, with `MCP_ALLOWED_HOSTS` genuinely unset.

    Every other MCP REST fixture sets it, because `localhost` is private and needed
    an entry — so nothing until now exercised the default a real deployment gets.
    That is the gap this slice is about, and it is the level at which "the feature
    is dead on arrival" was true.
    """
    from litestar.testing import AsyncTestClient
    from rbac.conftest import ADMIN_EMAIL, MASTER_KEY, _admin, _bearer, _team

    from litestar_gateway.app import create_app

    settings = Settings(
        database_url=database_url,
        admin_email=ADMIN_EMAIL,
        master_key=MASTER_KEY,
        jwt_secret="test-secret-key-0123456789-abcdefghij",  # pragma: allowlist secret
        salt_key="test-salt-key",
        # No mcp_allowed_hosts at all — the point of the test.
    )
    assert settings.mcp_allowlist().is_empty

    async with AsyncTestClient(app=create_app(settings)) as client:
        admin = await _admin(client)
        team = await _team(client, admin)

        created = await client.post(
            f"/teams/{team}/mcp-servers",
            json={"name": "hosted", "url": PUBLIC_URL},
            headers=_bearer(admin),
        )
        blocked = await client.post(
            f"/teams/{team}/mcp-servers",
            json={"name": "metadata", "url": "https://169.254.169.254/mcp"},
            headers=_bearer(admin),
        )

        assert created.status_code == HTTP_201_CREATED, created.text
        assert blocked.status_code == HTTP_400_BAD_REQUEST
        assert "MCP_ALLOWED_HOSTS" in blocked.text


async def test_an_empty_allowlist_does_not_skip_resolution() -> None:
    """The fall-through has to actually resolve, because the address is the thing
    being judged. Short-circuiting on "no allowlist, allow it" is the bug this
    asserts against — and it would pass every test above except the metadata one."""
    with pytest.raises(ValueError, match="blocked address"):
        await resolve_optionally_allowlisted_addresses("sneaky.example.com", 443, NO_ALLOWLIST)

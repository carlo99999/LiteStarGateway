"""Shared guarded-egress primitives.

Every outbound call the gateway makes to an operator-configured address goes
through the checks here, so there is exactly one implementation of "is this
target allowed" rather than one per caller.

Two policies, deliberately opposite, share the same resolution machinery:

- **Deny-list** (`resolve_approved_addresses`, R6-H18): the target must be a
  plain public unicast address. Used by the routing webhook strategy, the
  Plan 07 budget-alert channel and the guardrail webhook provider — an admin
  pointing any of those at `169.254.169.254` is an SSRF, never a use case.
- **Allow-list** (Plan 18, added alongside this module): the target must match
  an explicit operator allowlist, *including* private addresses. A gateway
  calling a self-hosted model server at `vllm.internal:8000` is the entire
  point, so the deny-list above is exactly the wrong check there.
- **Allow-list over deny-list** (`resolve_optionally_allowlisted_addresses`, the
  MCP tool gateway): the allowlist when an operator configured one, the deny-list
  when they did not. For a surface that should work against public endpoints with
  no configuration, where an allowlist entry is how you additionally authorize an
  internal target rather than how you authorize anything at all.

Both re-resolve on every call rather than trusting what was validated at
config-save time, which is what makes them resistant to DNS rebinding.

Extracted from `application/routing/webhook.py`, which was the de-facto home
for these while the deny-list was the only policy; behavior is unchanged.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

from litestar_gateway.domain.egress_policy import EgressAllowlist

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


async def _resolve_host_addresses(host: str) -> list[str]:
    """Async DNS resolution (module-level for test injection)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _literal_ip(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_blocked(ip: IPAddress) -> bool:
    """SSRF deny-list: anything that isn't a plain public unicast address."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _host_addresses(host: str) -> tuple[IPAddress, ...]:
    """Every address `host` currently resolves to, or the literal itself."""
    literal = _literal_ip(host)
    if literal is not None:
        return (literal,)
    resolved = await _resolve_host_addresses(host)
    addresses = tuple(ipaddress.ip_address(address) for address in resolved)
    if not addresses:
        raise ValueError(f"host {host!r} did not resolve to any address")
    return addresses


async def resolve_allowlisted_addresses(
    host: str, port: int | None, allowlist: EgressAllowlist
) -> tuple[IPAddress, ...]:
    """Allow-list guard (Plan 18), the deliberate opposite of the deny-list
    below: the target must match an operator-supplied entry, private addresses
    included. Re-resolved on every call, so a name that drifts out of an
    allowlisted range is refused at call time and not merely at config-save
    time.

    An empty allowlist refuses before resolving — a deployment that has not
    opted in must gain no new egress reach at all, and must not emit DNS
    traffic for a target it will refuse anyway."""
    if allowlist.is_empty:
        raise ValueError(
            f"host {host!r} is not permitted: no egress allowlist is configured. "
            "Set OPENAI_COMPATIBLE_ALLOWED_HOSTS to authorize this target."
        )
    addresses = await _host_addresses(host)
    if not allowlist.permits(host, port, addresses):
        raise ValueError(
            f"host {host!r} (port {port}) is not permitted by the egress allowlist; "
            "add it to OPENAI_COMPATIBLE_ALLOWED_HOSTS to authorize it"
        )
    return addresses


async def resolve_optionally_allowlisted_addresses(
    host: str, port: int | None, allowlist: EgressAllowlist
) -> tuple[IPAddress, ...]:
    """The third policy: **allowlist when configured, deny-list when not.**

    The two policies above are absolutes — one refuses everything without an
    explicit entry, the other refuses every non-public address. This composes them
    for a surface that wants to work out of the box against public endpoints while
    still letting an operator authorize an internal one.

    So an empty allowlist does *not* mean "anything goes": it falls through to the
    SSRF deny-list, and `169.254.169.254` is refused with no configuration at all.
    What an allowlist entry buys is the ability to reach a **private** target —
    which is the only thing the deny-list would otherwise stop, and exactly why
    `resolve_allowlisted_addresses` exists.

    Deliberately a separate function rather than a flag on that one. Inverting the
    polarity of the allowlist guard in place would silently loosen
    `OPENAI_COMPATIBLE_ALLOWED_HOSTS` too, where clearing the allowlist failing to
    stop existing credentials was Round 15's ISSUE-034.
    """
    if allowlist.is_empty:
        return await resolve_approved_addresses(host)
    return await resolve_allowlisted_addresses(host, port, allowlist)


async def resolve_approved_addresses(host: str) -> tuple[IPAddress, ...]:
    """SSRF guard (R6-H18), re-checked on every call to resist DNS rebinding
    between config-save and use: every address `host` resolves to must be
    public. A blocked target raises. Shared by every guarded-egress caller so
    they go through the exact same deny-list rather than a re-implementation
    of it."""
    addresses = await _host_addresses(host)
    for address in addresses:
        if _is_blocked(address):
            raise ValueError(
                f"host {host!r} resolves to blocked address {address}; "
                "only public endpoints are allowed"
            )
    return addresses

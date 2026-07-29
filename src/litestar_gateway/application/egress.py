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

"""Allowlist egress policy (Plan 18).

The deliberate opposite of the SSRF deny-list in `application/egress.py`: this
policy permits exactly the targets an operator named, **including private
ones**, because reaching a self-hosted model server at `vllm.internal:8000` is
the entire purpose of the provider it guards. Applying the deny-list there
would reject the only address anyone wants.

Pure: parsing and matching only. Resolution (DNS, which is I/O) lives in
`application/egress.py`, which combines the two.

Entry grammar, one per allowlist element:

    <target>[:<port>]

    <target> ::= hostname | IPv4 | IPv4 CIDR | IPv6 | IPv6 CIDR

An IPv6 target that carries a port must be bracketed (`[fd00::1]:8000`),
since an unbracketed IPv6 literal is all colons. Omitting the port permits
any port on that target.

Two kinds of entry, with deliberately different strength:

- A **name** entry authorizes a hostname. The operator is vouching for that
  name and its DNS, so the addresses it resolves to are not further
  constrained.
- An **address** entry (literal or CIDR) authorizes addresses. *Every* address
  the target resolves to must fall inside an allowlisted network — one listed
  sibling must not smuggle an unlisted address through a split DNS answer.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

_MIN_PORT = 1
_MAX_PORT = 65535


@dataclass(frozen=True)
class AllowlistEntry:
    """One parsed allowlist element. Exactly one of `hostname`/`network` is set."""

    port: int | None
    hostname: str | None = None
    network: IPNetwork | None = None

    def permits_port(self, port: int | None) -> bool:
        return self.port is None or self.port == port


@dataclass(frozen=True)
class EgressAllowlist:
    entries: tuple[AllowlistEntry, ...]

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def permits(self, host: str, port: int | None, addresses: tuple[IPAddress, ...]) -> bool:
        """True when `host`/`port` is authorized by a name entry, or when every
        address in `addresses` is authorized by an address entry."""
        if self.is_empty:
            return False
        target = host.strip().lower()
        if any(
            entry.hostname == target and entry.permits_port(port)
            for entry in self.entries
            if entry.hostname is not None
        ):
            return True
        networks = tuple(
            entry.network
            for entry in self.entries
            if entry.network is not None and entry.permits_port(port)
        )
        if not networks or not addresses:
            return False
        return all(any(address in network for network in networks) for address in addresses)


def _split_port(raw: str) -> tuple[str, int | None]:
    """Split `<target>[:<port>]`, honoring bracketed IPv6."""
    if raw.startswith("["):
        closing = raw.find("]")
        if closing == -1:
            raise ValueError(f"allowlist entry {raw!r} has an unclosed '['")
        target = raw[1:closing]
        remainder = raw[closing + 1 :]
        if not remainder:
            return target, None
        if not remainder.startswith(":"):
            raise ValueError(f"allowlist entry {raw!r} has trailing text after ']'")
        return target, _parse_port(remainder[1:], raw)
    # An unbracketed IPv6 literal or CIDR is all colons; a single colon is a port.
    if raw.count(":") == 1:
        target, _, port = raw.partition(":")
        return target, _parse_port(port, raw)
    return raw, None


def _parse_port(raw_port: str, entry: str) -> int:
    try:
        port = int(raw_port)
    except ValueError:
        raise ValueError(f"allowlist entry {entry!r} has a non-numeric port") from None
    if not _MIN_PORT <= port <= _MAX_PORT:
        raise ValueError(f"allowlist entry {entry!r} has a port outside 1-65535")
    return port


def _parse_entry(raw: str) -> AllowlistEntry:
    entry = raw.strip().lower()
    if not entry:
        raise ValueError("allowlist entry is empty")
    target, port = _split_port(entry)
    if not target:
        raise ValueError(f"allowlist entry {raw!r} has no target")
    try:
        # strict=False so a host address with a prefix ("10.42.0.1/16") is
        # accepted as the network it names rather than rejected on host bits.
        return AllowlistEntry(port=port, network=ipaddress.ip_network(target, strict=False))
    except ValueError:
        pass
    if "/" in target:
        # It looked like a CIDR and failed to parse as one — an invalid prefix
        # must not silently fall through and be treated as a hostname.
        raise ValueError(f"allowlist entry {raw!r} is not a valid network")
    return AllowlistEntry(port=port, hostname=target)


def parse_allowlist(raw_entries: Iterable[str]) -> EgressAllowlist:
    """Parse operator-supplied entries. Raises `ValueError` on any malformed
    element: this runs at the config boundary, where failing fast beats
    silently dropping an entry an operator believed was in force."""
    return EgressAllowlist(entries=tuple(_parse_entry(raw) for raw in raw_entries))

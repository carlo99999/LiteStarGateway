"""Plan 18 Phase 0 — the allowlist egress policy.

The deny-list guard (`resolve_approved_addresses`) refuses anything that is not
public. This policy is its deliberate opposite: it permits exactly the targets
an operator named, *including* private ones, because calling a self-hosted
model server on the cluster network is the whole point of the provider it
guards. The two must never be confused, so these tests pin both the permits
and the refuses.
"""

from __future__ import annotations

import ipaddress

import pytest

from litestar_gateway.application.egress import resolve_allowlisted_addresses
from litestar_gateway.domain.egress_policy import EgressAllowlist, parse_allowlist


def _addrs(*values: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    return tuple(ipaddress.ip_address(value) for value in values)


class TestParsing:
    def test_empty_input_yields_an_empty_allowlist(self) -> None:
        assert parse_allowlist(()).is_empty

    def test_entries_are_whitespace_trimmed_and_case_folded(self) -> None:
        allowlist = parse_allowlist((" VLLM.Internal ",))
        assert not allowlist.is_empty
        assert allowlist.permits("vllm.internal", 8000, _addrs("10.0.0.1"))

    @pytest.mark.parametrize(
        "raw",
        [
            "vllm.internal:notaport",
            "vllm.internal:0",
            "vllm.internal:70000",
            "10.42.0.0/33",
            "",
        ],
    )
    def test_a_malformed_entry_is_rejected_at_parse_time(self, raw: str) -> None:
        # Config boundary: fail fast at startup rather than silently dropping an
        # entry an operator believed was in force.
        with pytest.raises(ValueError):
            parse_allowlist((raw,))


class TestNameEntries:
    def test_hostname_entry_permits_any_port(self) -> None:
        allowlist = parse_allowlist(("vllm.internal",))
        assert allowlist.permits("vllm.internal", 8000, _addrs("10.0.0.1"))
        assert allowlist.permits("vllm.internal", 443, _addrs("10.0.0.1"))

    def test_hostname_port_entry_pins_the_port(self) -> None:
        allowlist = parse_allowlist(("vllm.internal:8000",))
        assert allowlist.permits("vllm.internal", 8000, _addrs("10.0.0.1"))
        assert not allowlist.permits("vllm.internal", 8001, _addrs("10.0.0.1"))

    def test_a_different_host_is_refused(self) -> None:
        allowlist = parse_allowlist(("vllm.internal",))
        assert not allowlist.permits("evil.example.com", 8000, _addrs("10.0.0.1"))

    def test_a_suffix_is_not_a_match(self) -> None:
        # "notvllm.internal" must not ride on an entry for "vllm.internal".
        allowlist = parse_allowlist(("vllm.internal",))
        assert not allowlist.permits("notvllm.internal", 8000, _addrs("10.0.0.1"))
        assert not allowlist.permits("vllm.internal.evil.com", 8000, _addrs("10.0.0.1"))


class TestAddressEntries:
    def test_cidr_entry_permits_a_contained_address(self) -> None:
        allowlist = parse_allowlist(("10.42.0.0/16",))
        assert allowlist.permits("anything.internal", 8000, _addrs("10.42.7.9"))

    def test_cidr_entry_refuses_an_address_outside_it(self) -> None:
        allowlist = parse_allowlist(("10.42.0.0/16",))
        assert not allowlist.permits("anything.internal", 8000, _addrs("10.43.7.9"))

    def test_every_resolved_address_must_match_not_merely_one(self) -> None:
        # A split DNS answer must not smuggle an unlisted address through on the
        # strength of a listed sibling.
        allowlist = parse_allowlist(("10.42.0.0/16",))
        assert not allowlist.permits("split.internal", 8000, _addrs("10.42.7.9", "203.0.113.5"))

    def test_literal_ip_entry(self) -> None:
        allowlist = parse_allowlist(("10.0.0.7",))
        assert allowlist.permits("h", 8000, _addrs("10.0.0.7"))
        assert not allowlist.permits("h", 8000, _addrs("10.0.0.8"))

    def test_ipv6_entry_is_bracketed_when_it_carries_a_port(self) -> None:
        allowlist = parse_allowlist(("[fd00::1]:8000",))
        assert allowlist.permits("h", 8000, _addrs("fd00::1"))
        assert not allowlist.permits("h", 8001, _addrs("fd00::1"))

    def test_bare_ipv6_entry_without_a_port(self) -> None:
        allowlist = parse_allowlist(("fd00::/8",))
        assert allowlist.permits("h", 443, _addrs("fd00::1"))

    def test_no_resolved_address_is_never_a_match(self) -> None:
        allowlist = parse_allowlist(("10.42.0.0/16",))
        assert not allowlist.permits("h", 8000, ())


class TestFailClosed:
    def test_an_empty_allowlist_permits_nothing(self) -> None:
        # The upgrade-safety property: a deployment that has not opted in gains
        # no new egress reach whatsoever.
        allowlist = EgressAllowlist(entries=())
        assert allowlist.is_empty
        assert not allowlist.permits("vllm.internal", 8000, _addrs("10.0.0.1"))
        assert not allowlist.permits("127.0.0.1", 8000, _addrs("127.0.0.1"))


class TestResolveAndCheck:
    """The async entry point: resolves, then applies the policy. Re-resolving
    per call is what makes a name that drifts out of an allowlisted range fail
    at call time rather than only at config-save time."""

    @pytest.mark.asyncio
    async def test_returns_the_resolved_addresses_when_permitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import litestar_gateway.application.egress as egress_module

        async def resolver(host: str) -> list[str]:
            return ["10.42.0.9"]

        monkeypatch.setattr(egress_module, "_resolve_host_addresses", resolver)
        addresses = await resolve_allowlisted_addresses(
            "vllm.internal", 8000, parse_allowlist(("10.42.0.0/16",))
        )
        assert addresses == _addrs("10.42.0.9")

    @pytest.mark.asyncio
    async def test_rebinding_out_of_the_allowlisted_range_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import litestar_gateway.application.egress as egress_module

        async def rebinding(host: str) -> list[str]:
            return ["169.254.169.254"]

        monkeypatch.setattr(egress_module, "_resolve_host_addresses", rebinding)
        with pytest.raises(ValueError, match="not permitted"):
            await resolve_allowlisted_addresses(
                "vllm.internal", 8000, parse_allowlist(("10.42.0.0/16",))
            )

    @pytest.mark.asyncio
    async def test_a_name_entry_does_not_require_the_address_to_be_listed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Naming a host is the operator vouching for that name's DNS; the
        # address it resolves to is then not separately constrained.
        import litestar_gateway.application.egress as egress_module

        async def resolver(host: str) -> list[str]:
            return ["198.51.100.7"]

        monkeypatch.setattr(egress_module, "_resolve_host_addresses", resolver)
        addresses = await resolve_allowlisted_addresses(
            "vllm.internal", 8000, parse_allowlist(("vllm.internal",))
        )
        assert addresses == _addrs("198.51.100.7")

    @pytest.mark.asyncio
    async def test_a_literal_target_needs_no_dns(self) -> None:
        addresses = await resolve_allowlisted_addresses(
            "10.42.0.9", 8000, parse_allowlist(("10.42.0.0/16",))
        )
        assert addresses == _addrs("10.42.0.9")

    @pytest.mark.asyncio
    async def test_an_empty_allowlist_refuses_before_resolving(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import litestar_gateway.application.egress as egress_module

        async def explode(host: str) -> list[str]:  # pragma: no cover
            raise AssertionError("must not resolve when the allowlist is empty")

        monkeypatch.setattr(egress_module, "_resolve_host_addresses", explode)
        with pytest.raises(ValueError, match="OPENAI_COMPATIBLE_ALLOWED_HOSTS"):
            await resolve_allowlisted_addresses("vllm.internal", 8000, EgressAllowlist(entries=()))

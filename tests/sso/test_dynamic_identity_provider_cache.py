"""Unit tests for `SsoIdentityProviderCache`: it must rebuild the wrapped
`OIDCIdentityProvider` only when the config fingerprint actually changes, so
unrelated requests keep its discovery/JWKS cache warm instead of refetching
the IdP on every login."""

from __future__ import annotations

from litestar_gateway.infrastructure.sso.dynamic import SsoIdentityProviderCache
from litestar_gateway.infrastructure.sso.oidc import OIDCIdentityProvider

_DISCOVERY = "https://idp.example.com/.well-known/openid-configuration"


def test_same_config_reuses_the_same_instance() -> None:
    cache = SsoIdentityProviderCache()
    first = cache.resolve(_DISCOVERY, "client-a", "secret-a", "openid email")
    second = cache.resolve(_DISCOVERY, "client-a", "secret-a", "openid email")
    assert first is second
    assert isinstance(first, OIDCIdentityProvider)


def test_changed_client_id_rebuilds() -> None:
    cache = SsoIdentityProviderCache()
    first = cache.resolve(_DISCOVERY, "client-a", "secret-a", "openid email")
    second = cache.resolve(_DISCOVERY, "client-b", "secret-a", "openid email")
    assert first is not second


def test_changed_secret_rebuilds() -> None:
    """A secret rotation must take effect — a stale cached client would keep
    authenticating with the old (possibly now-invalid) secret."""
    cache = SsoIdentityProviderCache()
    first = cache.resolve(_DISCOVERY, "client-a", "secret-a", "openid email")
    second = cache.resolve(_DISCOVERY, "client-a", "secret-b", "openid email")
    assert first is not second


def test_changed_discovery_url_rebuilds() -> None:
    cache = SsoIdentityProviderCache()
    first = cache.resolve(_DISCOVERY, "client-a", "secret-a", "openid email")
    second = cache.resolve(
        "https://other-idp.example.com/.well-known/openid-configuration",
        "client-a",
        "secret-a",
        "openid email",
    )
    assert first is not second


def test_reverting_to_a_prior_config_still_rebuilds() -> None:
    """No stale-instance history is kept — only the *last* fingerprint is
    remembered, so going back to an earlier config still rebuilds (correct,
    if slightly conservative: it just means a fresh discovery/JWKS fetch)."""
    cache = SsoIdentityProviderCache()
    first = cache.resolve(_DISCOVERY, "client-a", "secret-a", "openid email")
    cache.resolve(_DISCOVERY, "client-b", "secret-a", "openid email")
    third = cache.resolve(_DISCOVERY, "client-a", "secret-a", "openid email")
    assert first is not third

"""Unit tests for the real OIDCIdentityProvider adapter.

The IdP's HTTP surface (discovery, JWKS, token endpoint) is mocked; id_tokens are
signed with a generated RSA key so verification exercises the real joserfc path.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from litestar_test.domain.exceptions import SSOExchangeError
from litestar_test.infrastructure.sso.oidc import OIDCIdentityProvider, _claim_is_true

_ISSUER = "https://idp.example"
_CLIENT_ID = "client-abc"
_DISCOVERY_URL = f"{_ISSUER}/.well-known/openid-configuration"
_METADATA = {
    "issuer": _ISSUER,
    "authorization_endpoint": f"{_ISSUER}/authorize",
    "token_endpoint": f"{_ISSUER}/token",
    "jwks_uri": f"{_ISSUER}/jwks",
}
NONCE = "nonce-123"
VERIFIER = "a-code-verifier-that-is-long-enough-for-pkce-0123456789"


@pytest.fixture
def signing_key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": "k1"}, private=True)


def _seeded_provider(signing_key: RSAKey) -> OIDCIdentityProvider:
    """Provider with discovery + JWKS pre-seeded, so only token exchange hits IO."""
    provider = OIDCIdentityProvider(_DISCOVERY_URL, _CLIENT_ID, "secret", "openid email")
    provider._metadata = dict(_METADATA)
    provider._jwks = KeySet.import_key_set({"keys": [signing_key.as_dict(private=False)]})
    return provider


def _id_token(signing_key: RSAKey, **overrides: Any) -> str:
    claims: dict[str, Any] = {
        "iss": _ISSUER,
        "aud": _CLIENT_ID,
        "sub": "u1",
        "email": "a@corp.com",
        "email_verified": True,
        "groups": ["g1"],
        "nonce": NONCE,
    }
    claims.update(overrides)
    return jwt.encode({"alg": "RS256", "kid": "k1"}, claims, signing_key)


def _patch_fetch_token(monkeypatch: pytest.MonkeyPatch, token: dict[str, Any]) -> None:
    async def fake_fetch_token(self: Any, url: str, **kwargs: Any) -> dict[str, Any]:
        return token

    monkeypatch.setattr(AsyncOAuth2Client, "fetch_token", fake_fetch_token)


def _patch_http_get(monkeypatch: pytest.MonkeyPatch, by_url: dict[str, Any]) -> None:
    class FakeResponse:
        def __init__(self, data: Any) -> None:
            self._data = data

        def raise_for_status(self) -> None:
            return None

        def json(self) -> Any:
            return self._data

    async def fake_get(self: Any, url: str) -> FakeResponse:
        for fragment, data in by_url.items():
            if fragment in url:
                return FakeResponse(data)
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


def test_claim_is_true_accepts_bool_and_string() -> None:
    assert _claim_is_true(True) is True
    assert _claim_is_true("true") is True
    assert _claim_is_true("True") is True
    assert _claim_is_true(False) is False
    assert _claim_is_true("false") is False
    assert _claim_is_true(None) is False
    assert _claim_is_true("1") is False  # only a literal "true" counts


async def test_exchange_parses_identity(
    signing_key: RSAKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _seeded_provider(signing_key)
    # email_verified as the string "true" (some IdPs) must parse to bool True.
    _patch_fetch_token(
        monkeypatch,
        {"id_token": _id_token(signing_key, email_verified="true", groups=["g1", "admins"])},
    )
    identity = await provider.exchange(
        "code", "https://app/sso/callback", nonce=NONCE, code_verifier=VERIFIER
    )
    assert identity.subject == "u1"
    assert identity.email == "a@corp.com"
    assert identity.email_verified is True
    assert identity.groups == ("g1", "admins")


async def test_exchange_missing_id_token_raises(
    signing_key: RSAKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _seeded_provider(signing_key)
    _patch_fetch_token(monkeypatch, {"access_token": "at"})  # no id_token
    with pytest.raises(SSOExchangeError):
        await provider.exchange(
            "code", "https://app/sso/callback", nonce=NONCE, code_verifier=VERIFIER
        )


async def test_exchange_wrong_issuer_raises(
    signing_key: RSAKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _seeded_provider(signing_key)
    _patch_fetch_token(
        monkeypatch, {"id_token": _id_token(signing_key, iss="https://evil.example")}
    )
    with pytest.raises(SSOExchangeError):  # joserfc claim error, translated
        await provider.exchange(
            "code", "https://app/sso/callback", nonce=NONCE, code_verifier=VERIFIER
        )


async def test_exchange_wrong_audience_raises(
    signing_key: RSAKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _seeded_provider(signing_key)
    _patch_fetch_token(monkeypatch, {"id_token": _id_token(signing_key, aud="someone-else")})
    with pytest.raises(SSOExchangeError):
        await provider.exchange(
            "code", "https://app/sso/callback", nonce=NONCE, code_verifier=VERIFIER
        )


async def test_exchange_tampered_signature_raises(
    signing_key: RSAKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _seeded_provider(signing_key)
    # Sign with a *different* key than the one in the seeded JWKS.
    other = RSAKey.generate_key(2048, parameters={"kid": "k1"}, private=True)
    _patch_fetch_token(monkeypatch, {"id_token": _id_token(other)})
    with pytest.raises(SSOExchangeError):
        await provider.exchange(
            "code", "https://app/sso/callback", nonce=NONCE, code_verifier=VERIFIER
        )


async def test_full_discovery_and_jwks_http_flow(
    signing_key: RSAKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No pre-seeded caches: exercises _load_metadata and _fetch_key_set over HTTP.
    _patch_http_get(
        monkeypatch,
        {
            "well-known": _METADATA,
            "/jwks": {"keys": [signing_key.as_dict(private=False)]},
        },
    )
    _patch_fetch_token(monkeypatch, {"id_token": _id_token(signing_key)})
    provider = OIDCIdentityProvider(_DISCOVERY_URL, _CLIENT_ID, "secret", "openid email")
    identity = await provider.exchange(
        "code", "https://app/sso/callback", nonce=NONCE, code_verifier=VERIFIER
    )
    assert identity.subject == "u1"


async def test_discovery_missing_fields_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_http_get(monkeypatch, {"well-known": {"issuer": _ISSUER}})  # endpoints absent
    provider = OIDCIdentityProvider(_DISCOVERY_URL, _CLIENT_ID, "secret", "openid")
    with pytest.raises(SSOExchangeError):
        await provider._load_metadata()


async def test_authorization_url_is_built_from_discovery(
    signing_key: RSAKey,
) -> None:
    provider = _seeded_provider(signing_key)
    url = await provider.authorization_url(
        "state-123", "https://app/sso/callback", nonce=NONCE, code_verifier=VERIFIER
    )
    assert url.startswith(f"{_ISSUER}/authorize")
    assert "state=state-123" in url
    assert "client_id=client-abc" in url
    # nonce rides the query; PKCE sends the S256 challenge, never the verifier.
    assert f"nonce={NONCE}" in url
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert VERIFIER not in url


async def test_exchange_nonce_mismatch_raises(
    signing_key: RSAKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An id_token minted for a different authorization request must be rejected.
    provider = _seeded_provider(signing_key)
    _patch_fetch_token(monkeypatch, {"id_token": _id_token(signing_key, nonce="other-nonce")})
    with pytest.raises(SSOExchangeError):
        await provider.exchange(
            "code", "https://app/sso/callback", nonce=NONCE, code_verifier=VERIFIER
        )


async def test_exchange_sends_code_verifier(
    signing_key: RSAKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    async def fake_fetch_token(self: Any, url: str, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id_token": _id_token(signing_key)}

    monkeypatch.setattr(AsyncOAuth2Client, "fetch_token", fake_fetch_token)
    provider = _seeded_provider(signing_key)
    await provider.exchange("code", "https://app/sso/callback", nonce=NONCE, code_verifier=VERIFIER)
    assert captured["code_verifier"] == VERIFIER

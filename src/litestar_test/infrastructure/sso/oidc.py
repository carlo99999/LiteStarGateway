"""Generic OIDC identity provider (Authlib + joserfc).

Works with any OIDC provider that publishes a discovery document — Google,
Microsoft/Entra, Okta, Keycloak, … — by pointing `OIDC_DISCOVERY_URL` at its
`.well-known/openid-configuration`. Authlib handles the authorization-code
exchange; joserfc verifies the `id_token` against the provider's JWKS.

Confidential-client flow (a client secret is configured), so CSRF is covered by
the `state` parameter. PKCE and `nonce` are hardening follow-ups.
"""

from __future__ import annotations

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from joserfc import jwt
from joserfc.jwk import KeySet

from litestar_test.domain.entities import ExternalIdentity

# id_tokens are signed with the provider's asymmetric JWKS keys.
_ID_TOKEN_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]


class OIDCIdentityProvider:
    def __init__(
        self, discovery_url: str, client_id: str, client_secret: str | None, scopes: str
    ) -> None:
        self._discovery_url = discovery_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = scopes
        self._metadata: dict | None = None

    async def _load_metadata(self) -> dict:
        if self._metadata is None:
            async with httpx.AsyncClient() as http:
                resp = await http.get(self._discovery_url)
                resp.raise_for_status()
                self._metadata = resp.json()
        return self._metadata

    def _client(self, redirect_uri: str) -> AsyncOAuth2Client:
        return AsyncOAuth2Client(
            self._client_id, self._client_secret, scope=self._scopes, redirect_uri=redirect_uri
        )

    async def authorization_url(self, state: str, redirect_uri: str) -> str:
        metadata = await self._load_metadata()
        url, _ = self._client(redirect_uri).create_authorization_url(
            metadata["authorization_endpoint"], state=state
        )
        return url

    async def exchange(self, code: str, redirect_uri: str) -> ExternalIdentity:
        metadata = await self._load_metadata()
        token = await self._client(redirect_uri).fetch_token(
            metadata["token_endpoint"], code=code, grant_type="authorization_code"
        )
        claims = await self._verify_id_token(token["id_token"], metadata)
        groups = claims.get("groups") or []
        return ExternalIdentity(
            subject=str(claims["sub"]),
            email=str(claims.get("email") or ""),
            groups=tuple(str(g) for g in groups),
        )

    async def _verify_id_token(self, id_token: str, metadata: dict) -> dict:
        async with httpx.AsyncClient() as http:
            resp = await http.get(metadata["jwks_uri"])
            resp.raise_for_status()
            jwks = resp.json()
        decoded = jwt.decode(id_token, KeySet.import_key_set(jwks), algorithms=_ID_TOKEN_ALGS)
        jwt.JWTClaimsRegistry(
            iss={"essential": True, "value": metadata["issuer"]},
            aud={"essential": True, "value": self._client_id},
        ).validate(decoded.claims)
        return decoded.claims

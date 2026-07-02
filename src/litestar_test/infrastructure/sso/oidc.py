"""Generic OIDC identity provider (Authlib + joserfc).

Works with any OIDC provider that publishes a discovery document — Google,
Microsoft/Entra, Okta, Keycloak, … — by pointing `OIDC_DISCOVERY_URL` at its
`.well-known/openid-configuration`. Authlib handles the authorization-code
exchange; joserfc verifies the `id_token` against the provider's JWKS.

Confidential-client flow (a client secret is required — see `Settings.sso_enabled`).
CSRF is covered by the `state` parameter; the `nonce` claim binds the id_token to
this authorization request (replay defense) and PKCE (S256) protects the
authorization code against interception. Both are verified on exchange.

Failures during discovery/exchange/verification are translated to the domain
`SSOExchangeError` (→ 401) so a misconfigured or flaky IdP surfaces as an auth
failure rather than a 500 that leaks the provider's internals.
"""

from __future__ import annotations

import httpx
from authlib.common.errors import AuthlibBaseError
from authlib.integrations.httpx_client import AsyncOAuth2Client
from joserfc import jwt
from joserfc.errors import InvalidKeyIdError, JoseError
from joserfc.jwk import KeySet

from litestar_test.domain.entities import ExternalIdentity
from litestar_test.domain.exceptions import SSOExchangeError

# id_tokens are signed with the provider's asymmetric JWKS keys.
_ID_TOKEN_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]

# Discovery-document fields we depend on; validated once so a malformed document
# fails clearly instead of raising a bare KeyError deep in the flow.
_REQUIRED_METADATA = ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer")


def _claim_is_true(value: object) -> bool:
    """OIDC booleans arrive as a JSON bool or, from some IdPs, the string "true"."""
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


class OIDCIdentityProvider:
    def __init__(
        self, discovery_url: str, client_id: str, client_secret: str | None, scopes: str
    ) -> None:
        self._discovery_url = discovery_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = scopes
        self._metadata: dict | None = None
        # Cached JWKS key set; refreshed lazily on an unknown-`kid` miss (keys rotate).
        self._jwks: KeySet | None = None

    async def _load_metadata(self) -> dict:
        if self._metadata is None:
            try:
                async with httpx.AsyncClient() as http:
                    resp = await http.get(self._discovery_url)
                    resp.raise_for_status()
                    metadata = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise SSOExchangeError("could not load OIDC discovery document") from exc
            missing = [k for k in _REQUIRED_METADATA if not metadata.get(k)]
            if missing:
                raise SSOExchangeError(
                    f"OIDC discovery document missing fields: {', '.join(missing)}"
                )
            self._metadata = metadata
        return self._metadata

    def _client(self, redirect_uri: str) -> AsyncOAuth2Client:
        return AsyncOAuth2Client(
            self._client_id,
            self._client_secret,
            scope=self._scopes,
            redirect_uri=redirect_uri,
            code_challenge_method="S256",
        )

    async def authorization_url(
        self, state: str, redirect_uri: str, *, nonce: str, code_verifier: str
    ) -> str:
        metadata = await self._load_metadata()
        # AsyncOAuth2Client owns an httpx connection pool; close it after each use
        # so logins don't leak sockets/file descriptors.
        client = self._client(redirect_uri)
        try:
            # Authlib derives the S256 code_challenge from the verifier; `nonce`
            # is appended to the query and echoed back inside the id_token.
            url, _ = client.create_authorization_url(
                metadata["authorization_endpoint"],
                state=state,
                nonce=nonce,
                code_verifier=code_verifier,
            )
        finally:
            await client.aclose()  # type: ignore[missing-attribute]  # httpx.AsyncClient base; authlib stub omits it
        return url

    async def exchange(
        self, code: str, redirect_uri: str, *, nonce: str, code_verifier: str
    ) -> ExternalIdentity:
        metadata = await self._load_metadata()
        try:
            client = self._client(redirect_uri)
            try:
                token = await client.fetch_token(
                    metadata["token_endpoint"],
                    code=code,
                    grant_type="authorization_code",
                    code_verifier=code_verifier,
                )
            finally:
                await client.aclose()  # type: ignore[missing-attribute]  # httpx.AsyncClient base; authlib stub omits it
            id_token = token.get("id_token")
            if not id_token:
                raise SSOExchangeError("token response did not include an id_token")
            claims = await self._verify_id_token(id_token, metadata)
        except (httpx.HTTPError, AuthlibBaseError, JoseError, ValueError, KeyError) as exc:
            raise SSOExchangeError("SSO authorization-code exchange failed") from exc
        # The id_token must echo our nonce, or it was minted for a different
        # authorization request (replay/injection).
        if claims.get("nonce") != nonce:
            raise SSOExchangeError("id_token nonce mismatch")
        groups = claims.get("groups") or []
        return ExternalIdentity(
            subject=str(claims["sub"]),
            email=str(claims.get("email") or ""),
            email_verified=_claim_is_true(claims.get("email_verified")),
            groups=tuple(str(g) for g in groups),
        )

    async def _fetch_key_set(self, metadata: dict) -> KeySet:
        async with httpx.AsyncClient() as http:
            resp = await http.get(metadata["jwks_uri"])
            resp.raise_for_status()
            return KeySet.import_key_set(resp.json())

    async def _verify_id_token(self, id_token: str, metadata: dict) -> dict:
        if self._jwks is None:
            self._jwks = await self._fetch_key_set(metadata)
        try:
            decoded = jwt.decode(id_token, self._jwks, algorithms=_ID_TOKEN_ALGS)
        except InvalidKeyIdError:
            # The token was signed with a key we don't have cached — the IdP likely
            # rotated its JWKS. Refresh once and retry before giving up.
            self._jwks = await self._fetch_key_set(metadata)
            decoded = jwt.decode(id_token, self._jwks, algorithms=_ID_TOKEN_ALGS)
        jwt.JWTClaimsRegistry(
            iss={"essential": True, "value": metadata["issuer"]},
            aud={"essential": True, "value": self._client_id},
        ).validate(decoded.claims)
        return decoded.claims

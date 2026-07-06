"""Port — SSO identity provider."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from litestar_gateway.domain.entities import ExternalIdentity


@runtime_checkable
class IdentityProvider(Protocol):
    """SSO provider: build the login redirect and resolve the callback code.

    `nonce` binds the id_token to this authorization request (replay defense);
    `code_verifier` is the PKCE secret whose S256 challenge rides the redirect
    (authorization-code interception defense). Both are generated per login and
    verified on exchange."""

    async def authorization_url(
        self, state: str, redirect_uri: str, *, nonce: str, code_verifier: str
    ) -> str: ...

    async def exchange(
        self, code: str, redirect_uri: str, *, nonce: str, code_verifier: str
    ) -> ExternalIdentity: ...

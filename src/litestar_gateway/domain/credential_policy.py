"""Creation-time validation of credential ``values`` per provider.

Mirrors the field contract documented on ``POST /credentials`` (and the keys
each LLM adapter actually reads). Providers without an entry are accepted
without validation.
"""

from __future__ import annotations

from litestar_gateway.domain.entities.enums import Provider
from litestar_gateway.domain.exceptions import CredentialMisconfigured

# provider -> (required keys, optional keys)
_FIELDS: dict[Provider, tuple[frozenset[str], frozenset[str]]] = {
    Provider.OPENAI: (frozenset({"api_key"}), frozenset({"api_base", "organization"})),
    Provider.ANTHROPIC: (frozenset({"api_key"}), frozenset({"api_base"})),
    Provider.AZURE_OPENAI: (
        frozenset({"api_key", "api_base", "api_version"}),
        frozenset({"deployment"}),
    ),
    # vertex_credentials is optional: without it the adapter falls back to
    # Application Default Credentials.
    Provider.VERTEX_AI: (
        frozenset({"vertex_project", "vertex_location"}),
        frozenset({"vertex_credentials"}),
    ),
    Provider.DATABRICKS: (frozenset({"api_key", "api_base"}), frozenset()),
    Provider.BEDROCK: (
        frozenset({"region", "aws_access_key_id", "aws_secret_access_key"}),
        frozenset({"aws_session_token"}),
    ),
}


def validate_credential_values(provider: Provider, values: dict[str, str]) -> None:
    """Raise ``CredentialMisconfigured`` when ``values`` does not match the
    provider's field contract (missing/blank required keys, unexpected keys)."""
    fields = _FIELDS.get(provider)
    if fields is None:
        return
    required, optional = fields
    missing = sorted(key for key in required if not values.get(key))
    unexpected = sorted(set(values) - required - optional)
    problems = []
    if missing:
        problems.append(f"missing required keys: {', '.join(missing)}")
    if unexpected:
        problems.append(f"unexpected keys: {', '.join(unexpected)}")
    if problems:
        raise CredentialMisconfigured(
            f"invalid values for provider '{provider}': {'; '.join(problems)}"
        )

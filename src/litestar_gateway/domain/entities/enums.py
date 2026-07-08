"""Domain enumerations."""

from enum import StrEnum


class TeamRole(StrEnum):
    """A user's role in one team. `admin` and `member` are the classic pair;
    the extended roles grant a member one capability domain (see
    `domain/authorization.py` for the role → permission mapping)."""

    ADMIN = "admin"
    MEMBER = "member"
    MODEL_MANAGER = "model-manager"
    KEY_ISSUER = "key-issuer"
    BILLING_VIEWER = "billing-viewer"


class KeyPurpose(StrEnum):
    """What a keyring key is used for."""

    CREDENTIAL = "credential"  # encrypts credential values at rest
    JWT = "jwt"  # signs login JWTs


class KeyScope(StrEnum):
    """What an API key may do. The key is a team-owned service principal;
    its scope bounds it to inference, team management, or both."""

    INFERENCE = "inference"  # the /v1/* endpoints only (default)
    MANAGEMENT = "management"  # team-scoped management, own team only
    ALL = "all"

    @property
    def allows_inference(self) -> bool:
        return self in (KeyScope.INFERENCE, KeyScope.ALL)

    @property
    def allows_management(self) -> bool:
        return self in (KeyScope.MANAGEMENT, KeyScope.ALL)


class BudgetWindow(StrEnum):
    """Spend window a budget applies to. Calendar-based, UTC."""

    MONTHLY = "monthly"
    DAILY = "daily"


class Provider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    VERTEX_AI = "vertex_ai"
    AZURE_OPENAI = "azure_openai"
    BEDROCK = "bedrock"
    DATABRICKS = "databricks"

    @property
    def honors_n(self) -> bool:
        """Whether this provider's chat path forwards the OpenAI `n` (multiple
        completions) upstream. The OpenAI-compatible providers splat the request
        into the SDK, so `n` is honored; the Anthropic/Vertex/Bedrock
        translators never read `n` and always return exactly one completion.
        Requesting n>1 there is rejected rather than silently under-delivering
        and over-reserving budget by up to MAX_N× (R7-M50)."""
        return self in _PROVIDERS_HONORING_N


# OpenAI-compatible surfaces (OpenAI, Azure, Databricks share the OpenAI SDK).
_PROVIDERS_HONORING_N = frozenset({Provider.OPENAI, Provider.AZURE_OPENAI, Provider.DATABRICKS})


class ModelType(StrEnum):
    CHAT = "chat"
    IMAGE = "image"
    EMBEDDINGS = "embeddings"

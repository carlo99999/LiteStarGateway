"""Default per-token pricing lookup, to prefill a model's costs in the console.

A bundled snapshot of the community LiteLLM price map, trimmed to the providers
this gateway supports and to just the two cost fields. Bundled (not fetched at
runtime) so the lookup works air-gapped and adds no network dependency.
"""

from litestar_gateway.infrastructure.pricing.catalog import (
    ModelPrice,
    ModelPriceCatalog,
)

__all__ = ["ModelPrice", "ModelPriceCatalog"]

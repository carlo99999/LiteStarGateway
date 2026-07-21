"""Load the bundled price snapshot and look up default costs by model id.

The snapshot is `{ provider: { normalized_model_id: {input, output} } }`, where
`normalized_model_id` is the LiteLLM key with any leading `"<vendor>/"` prefix
stripped so it lines up with the `provider_model_id` an admin actually enters
(e.g. LiteLLM's `azure/gpt-4o` → `gpt-4o`). Lookup is best-effort and exact:
a hit prefills the console form, a miss just leaves the fields blank.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from litestar_gateway.domain.entities import Provider

_DATA_FILE = Path(__file__).with_name("model_prices.json")


@dataclass(frozen=True)
class ModelPrice:
    input_cost_per_token: float
    output_cost_per_token: float


@lru_cache(maxsize=1)
def _load() -> dict[str, dict[str, ModelPrice]]:
    raw = json.loads(_DATA_FILE.read_text())
    return {
        provider: {
            model_id: ModelPrice(
                input_cost_per_token=costs["input_cost_per_token"],
                output_cost_per_token=costs["output_cost_per_token"],
            )
            for model_id, costs in models.items()
        }
        for provider, models in raw.items()
    }


class ModelPriceCatalog:
    """Default per-token costs, keyed by provider + upstream model id."""

    def lookup(self, provider: Provider, provider_model_id: str) -> ModelPrice | None:
        """The bundled default costs for this model, or None if unknown."""
        return _load().get(str(provider), {}).get(provider_model_id)

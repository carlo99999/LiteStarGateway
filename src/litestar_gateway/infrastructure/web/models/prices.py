"""Default per-token pricing lookup for the console's model forms.

`GET /model-prices?provider=…&provider_model_id=…` returns the bundled default
costs for a model, so the create/edit dialogs can prefill them (LiteLLM-style)
instead of making the admin hunt for the numbers. Reference data, not
team-scoped; still behind the same principal auth as the rest of management so
it isn't an open scraping surface. A model with no bundled price yields 404,
which the console treats as "no default — enter manually".
"""

from __future__ import annotations

from dataclasses import dataclass

from litestar import Controller, get
from litestar.di import NamedDependency, Provide
from litestar.exceptions import NotFoundException
from litestar.params import FromQuery

from litestar_gateway.domain.entities import Principal, Provider
from litestar_gateway.infrastructure.pricing import ModelPriceCatalog
from litestar_gateway.infrastructure.web.principal import provide_principal


@dataclass(frozen=True)
class ModelPriceResponse:
    provider: Provider
    provider_model_id: str
    input_cost_per_token: float
    output_cost_per_token: float


def provide_price_catalog() -> ModelPriceCatalog:
    return ModelPriceCatalog()


class ModelPricesController(Controller):
    path = "/model-prices"
    tags = ["models"]
    dependencies = {
        "principal": Provide(provide_principal),
        "catalog": Provide(provide_price_catalog, sync_to_thread=False),
    }

    @get(
        "/",
        summary="Default per-token costs for a model",
        description=(
            "Bundled default input/output costs for `provider` + `provider_model_id`, "
            "to prefill the model form. 404 when the model has no bundled price."
        ),
    )
    async def get_price(
        self,
        provider: FromQuery[Provider],
        provider_model_id: FromQuery[str],
        principal: NamedDependency[Principal],
        catalog: NamedDependency[ModelPriceCatalog],
    ) -> ModelPriceResponse:
        price = catalog.lookup(provider, provider_model_id)
        if price is None:
            raise NotFoundException("No bundled price for this model")
        return ModelPriceResponse(
            provider=provider,
            provider_model_id=provider_model_id,
            input_cost_per_token=price.input_cost_per_token,
            output_cost_per_token=price.output_cost_per_token,
        )

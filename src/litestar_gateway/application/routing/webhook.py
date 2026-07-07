"""S2 — external webhook strategy.

The admin points the router at their own HTTP endpoint; the gateway does not
care what's behind it (a heuristic, an ML model, a random pick). Contract
(documented in docs/routing-webhook.md):

    POST <url>                          timeout: `timeout_ms` (default 2000)
    { "task": "<user text>", "system_prompt": "<or null>",
      "models": ["m1", "m2", ...], "metadata": { "estimated_tokens": 123 } }

    → 200 { "choice": 2 }               1-based index into "models",
                                        or { "choice": "m2" } by name

Anything else — non-2xx, timeout, malformed body, out-of-range index, unknown
name — raises, and the caller falls back to `default_model` per §4. Validation
is strict at this boundary: a sloppy webhook must never steer silently.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

import httpx

from litestar_gateway.domain.routing import CandidateModel, RoutingContext, RoutingDecision

STRATEGY_ID = "webhook"
DEFAULT_TIMEOUT_MS = 2000


def _client_factory(timeout_seconds: float) -> httpx.AsyncClient:
    """Module-level for test injection."""
    return httpx.AsyncClient(timeout=timeout_seconds)


class WebhookStrategy:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        url = config.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("webhook strategy requires an http(s) 'url' in strategy_config")
        self._url = url
        self._bearer_token = config.get("bearer_token")
        self._timeout_seconds = config.get("timeout_ms", DEFAULT_TIMEOUT_MS) / 1000

    async def select(
        self, ctx: RoutingContext, candidates: tuple[CandidateModel, ...]
    ) -> RoutingDecision:
        start = perf_counter()
        names = [candidate.model_name for candidate in candidates]
        payload = {
            "task": ctx.user_text,
            "system_prompt": ctx.system_prompt,
            "models": names,
            "metadata": {"estimated_tokens": ctx.estimated_input_tokens},
        }
        headers = {"Authorization": f"Bearer {self._bearer_token}"} if self._bearer_token else {}
        async with _client_factory(self._timeout_seconds) as client:
            response = await client.post(self._url, json=payload, headers=headers)
        response.raise_for_status()
        chosen = self._parse_choice(response.json(), names)
        return RoutingDecision(
            model_name=chosen,
            strategy=STRATEGY_ID,
            tier=None,
            score=None,
            signals=(f"webhook chose {chosen}",),
            decision_ms=(perf_counter() - start) * 1000,
        )

    @staticmethod
    def _parse_choice(body: Any, names: list[str]) -> str:
        """Strict boundary validation: exactly {"choice": <1-based int | name>}."""
        if not isinstance(body, dict) or "choice" not in body:
            raise ValueError(f"webhook response missing 'choice': {body!r}")
        choice = body["choice"]
        # bool is an int subclass — reject it explicitly.
        if isinstance(choice, int) and not isinstance(choice, bool):
            if not 1 <= choice <= len(names):
                raise ValueError(f"webhook choice {choice} out of range 1..{len(names)}")
            return names[choice - 1]
        if isinstance(choice, str):
            if choice not in names:
                raise ValueError(f"webhook chose unknown model {choice!r}")
            return choice
        raise ValueError(f"webhook 'choice' must be a 1-based index or a model name: {choice!r}")

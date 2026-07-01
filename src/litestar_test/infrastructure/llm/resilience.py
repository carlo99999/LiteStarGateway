"""Resilience config applied to provider SDK clients.

A gateway sits in the critical path to external providers it does not control, so
a slow or failing upstream must fail *fast* and *bounded* rather than hanging (the
OpenAI/Anthropic SDK default timeout is ~10 minutes). The OpenAI and Anthropic
clients honour `timeout` + `max_retries` natively (exponential backoff, Retry-After,
correct streaming handling), so we configure them rather than hand-rolling a retry
loop; the Vertex/genai client takes a timeout via `HttpOptions` (milliseconds).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResilienceConfig:
    timeout: float = 60.0
    max_retries: int = 2

    @property
    def client_kwargs(self) -> dict[str, Any]:
        """Kwargs accepted by the OpenAI and Anthropic client constructors."""
        return {"timeout": self.timeout, "max_retries": self.max_retries}

    @property
    def timeout_ms(self) -> int:
        """Timeout in milliseconds, as the google-genai `HttpOptions` expects."""
        return int(self.timeout * 1000)

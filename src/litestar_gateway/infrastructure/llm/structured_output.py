"""Translate the OpenAI-shaped `response_format` into a provider-neutral spec.

OpenAI/Azure accept `response_format` natively; the other providers need it
mapped (Gemini → `response_schema`, Anthropic → a forced tool). This parses the
two request shapes once so each adapter shares the same interpretation:

  {"type": "json_object"}                          -> StructuredOutput(schema=None)
  {"type": "json_schema", "json_schema": {          -> StructuredOutput(schema=..)
      "name": "...", "schema": {...}, "strict": ...}}
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Tool/field name used when the client didn't name its schema.
_DEFAULT_NAME = "structured_output"


@dataclass(frozen=True)
class StructuredOutput:
    """A requested structured-output constraint, normalized across providers.

    `schema` is the JSON Schema for a `json_schema` request, or None for a bare
    `json_object` request (any valid JSON, no schema)."""

    name: str
    schema: dict[str, Any] | None


def parse_response_format(request: dict[str, Any]) -> StructuredOutput | None:
    """Return the structured-output spec for a request, or None if it didn't ask
    for one (or asked in an unrecognized shape — left to the provider)."""
    rf = request.get("response_format")
    if not isinstance(rf, dict):
        return None
    kind = rf.get("type")
    if kind == "json_object":
        return StructuredOutput(name=_DEFAULT_NAME, schema=None)
    if kind == "json_schema":
        js = rf.get("json_schema") if isinstance(rf.get("json_schema"), dict) else {}
        schema = js.get("schema")
        name = js.get("name") or _DEFAULT_NAME
        return StructuredOutput(
            name=str(name),
            schema=schema if isinstance(schema, dict) else None,
        )
    return None

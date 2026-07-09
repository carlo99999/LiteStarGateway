"""Provider-native passthrough endpoints (Anthropic Messages, Gemini …).

These serve each provider's *native* wire protocol so a customer can point the
provider's own SDK at the gateway. They are registered on the protected
`api_router`, inheriting the exact same per-IP rate limit and API-key auth as the
OpenAI-compatible endpoints — the guards are wired once, on the router, and
reused here by composition rather than re-declared.
"""

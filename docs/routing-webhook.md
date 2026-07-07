# Smart routing — external webhook strategy (S2)

With `"strategy": "webhook"` a router delegates the choice of the candidate
model to **your own HTTP endpoint**. The gateway does not care what is behind
it — a heuristic, your own ML classifier, a random pick.

## Router config

```json
{
  "name": "auto",
  "default_model": "big-model",
  "strategy": "webhook",
  "strategy_config": {
    "url": "https://picker.internal/route",
    "bearer_token": "optional-secret",
    "timeout_ms": 2000
  },
  "candidates": [ ... ]
}
```

`url` (http/https) is required — router creation fails without it.
`bearer_token` is sent as `Authorization: Bearer <token>` when set.

## The contract your endpoint implements

Request (POST, JSON):

```json
{
  "task": "<the user's last message text>",
  "system_prompt": "<the last system prompt, or null>",
  "models": ["cheap-model", "big-model"],
  "metadata": { "estimated_tokens": 123 }
}
```

`models` contains only the candidates that survived the gateway's hard
capability filters (vision/tools/json_schema/context window) — choose among
these only.

Response (200, JSON) — either form:

```json
{ "choice": 2 }            // 1-based index into "models"
{ "choice": "big-model" }  // or by name
```

## Failure semantics

Strict validation at the boundary: non-2xx, timeout (`timeout_ms`, default
2000 ms), malformed body, out-of-range index, or an unknown model name are all
treated as a strategy failure — the gateway logs it and routes to the router's
`default_model`. **A broken webhook never fails a user request**; it only
degrades routing to the default. Decisions carry `fallback_used=true` in the
`routing_decision` log so you can monitor how often your endpoint misbehaves.

## Shadow mode (§6)

Any strategy — including the webhook — can also run as a **shadow**: set
`"shadow_strategy": "webhook"` (its config under `strategy_config.shadow`).
The shadow runs fire-and-forget after the active strategy decides, never
blocking or failing the request; its would-be decision is persisted alongside
the real one (`is_shadow=true`, same table) so you can validate a new strategy
against live traffic before activating it.

# Design doc — Weighted multi-model routing

> **Status: implemented — but not as designed below.** This doc predates the
> smart-routing framework (`docs/next-steps/smart-routing.md`); once that
> Router/Strategy infrastructure existed, weighted routing became a strategy
> inside it (`application/routing/weighted.py`) rather than a standalone
> `ModelBlend` entity. Concretely: `CandidateModel.weight` replaces the
> `members: list[(model_id, weight)]` field below, and the existing
> `/teams/{id}/routers` CRUD, RBAC, audit, decision log, and shadow mode are
> reused as-is — no new entity, repository, service, or endpoints. The rest of
> this doc is kept for the original reasoning (open decisions §5 are resolved:
> the response echoes the actual chosen model, cost/usage already attributes
> to it, and streaming already works unchanged — all inherited for free from
> the shared router infrastructure).

## 1. Goal

Let a team expose a single alias that splits traffic across **up to 5 models by
percentage** — e.g. 50/50 between `gpt-5.5` and `claude-opus-4.8`, so half the
requests go to one model and half to the other. (A/B testing, gradual rollout,
cost blending.)

## 2. Concept

A new team-scoped entity, a **`ModelBlend`** (a routing alias):

- `id, team_id, name, enabled`
- `members: list[(model_id, weight)]` — 1..5 members, weights > 0.

Validation:

- ≤ 5 members; weights positive; normalized to 100% (or normalized internally).
- All member models belong to the same team and share the **same `ModelType`**
  (mixing chat + embeddings makes no sense).

## 3. Where it hooks

Routing is a thin layer **in front of model resolution** in `CompletionService`.
`_prepare` resolves `request["model"]`:

1. If the alias is a normal `Model` → today's behavior.
2. If it's a `ModelBlend` → pick a member via **weighted selection**, then resolve
   that member `Model` and continue exactly as today.

```text
domain/entities.py            ModelBlend
domain/ports.py               ModelBlendRepository
domain/routing.py             pure: choose_member(members, r) -> model_id
application/blend_service.py  CRUD + validation (team-admin scoped, like models)
application/completion_service.py   resolve blend -> member -> existing flow
infrastructure/web/...        /teams/{id}/blends CRUD; usable as a model alias
```

Keep selection a **pure function** taking an injected random draw `r ∈ [0,1)` so
it is deterministic in tests.

## 4. Selection

- Default: **weighted random** per request (`secrets`/`random`).
- Skip disabled members and re-normalize over the remainder.
- (Future) **sticky routing**: hash a stable key (user id / session) so the same
  caller consistently lands on the same member — needed for coherent multi-turn.

## 5. Open decisions

1. **What `model` is echoed** in the response: the blend alias, or the actual
   underlying model? Lean: actual model id, plus a trace attribute noting the
   blend. (ties to the observability doc.)
2. **Stickiness**: pure random (simple) vs sticky-by-user (better UX for chat).
3. **Cost/usage** must be attributed to the chosen member (not the alias).
4. **Streaming**: works unchanged (pick member, then stream from it).
5. **Shared routing abstraction** with smart-routing (`smart-routing.md`): both
   are "pick a model, then delegate" — consider one `Router` port with weighted
   and judge-based implementations.

## 6. Testing

- Pure `choose_member` tests: weight boundaries, disabled re-normalization,
  single member, deterministic draw.
- Service: validation (≤5, same type, same team, weights).
- Integration: a blend alias routes a chat call to a faked member SDK; repeated
  calls with controlled draws hit the expected split.

## 7. Rollout

1. `feat/model-blend` — entity + repo + CRUD + validation.
2. `feat/blend-routing` — `choose_member` + `CompletionService` integration +
   tests (chat first; other ops follow since they share resolution).

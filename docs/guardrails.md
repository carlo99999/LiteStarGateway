# Guardrails

A guardrail inspects a prompt before the gateway sends it, or an answer before
the gateway returns it, and decides: **allow**, **redact**, or **block**.

Off by default. A team with no rules behaves exactly as it did before guardrails
existed — the call path is unchanged, not merely permissive.

## The chain

A team's guardrails are an ordered list. Each rule names one provider, one
direction, and what its own failure means.

```text
POST /v1/chat/completions
  ├─ request chain   → allow / redact / block      ← before any budget is reserved
  ├─ provider call                                  (only if not blocked)
  ├─ settlement      → usage + cost recorded
  └─ response chain  → allow / redact / block      ← after the call is billed
```

Two placements deserve stating plainly, because they are what the design is:

- **the request chain runs before admission.** A blocked prompt never reaches a
  provider, so it reserves none of the team's budget. Otherwise a caller sending
  blocked prompts in a loop could squeeze real traffic out of the fleet-wide
  in-flight total without spending a token.
- **the response chain runs after settlement.** The provider call already
  happened and its tokens were really consumed, so a blocked answer is **still
  billed**. Not billing it would hand anyone who can trip the response guardrail
  a free channel.

A blocked request answers **422** — not 400, so a policy refusal is
distinguishable from a malformed request in your metrics.

### How verdicts combine

1. **Any block wins.** One provider refusing is enough; the others cannot
   overturn it.
2. **Redactions compose in order.** Each redacting provider rewrites the text and
   the next one sees the rewritten version. Order is `(position, name)` —
   deterministic, because two redactors can otherwise produce different text
   depending on which finished first.
3. **Allow is the identity.**

Rule 2 is why the chain runs **one provider at a time**, in order, each seeing
what the previous one left behind. Providers used to run concurrently on the
original text, which cannot compose: with two redactors the text that survived
was whichever rewrite finished last, and the other's redaction was silently
restored. A refusal also short-circuits the rest of the chain now, and a check
can act on what an earlier redaction exposed.

The cost is latency — a chain of N providers pays the sum of their times, not the
slowest — which is why every provider is time-bounded and chains are meant to be
short.

### Redaction is applied exactly, or not at all

A redaction is written back only where the mapping is unambiguous — a single
string message content. A chat message may carry a list of multimodal blocks, and
"here is the redacted flat text" does not say which block each piece came from.
So **a redact verdict on a shape the gateway cannot rewrite is escalated to a
block**, never passed through unredacted. Failing closed is the only safe
direction for a control that exists to keep content from leaving.

## Providers

### `webhook`

Asks an endpoint you run for a verdict. This is the one provider that sends the
user's prompt off the gateway, so it carries a sender's obligations:

- **signed** — HMAC-SHA256 over `"{timestamp}.{body}"` in `X-Gateway-Signature`,
  with a per-endpoint secret. See [webhook-contract.md](webhook-contract.md) for
  how to verify.
- **identified** — a stable `X-Gateway-Event-Id`, so a receiver that sees the
  same check twice can tell.
- **bounded** — `timeout_ms`, default 2000, max 10000. A guardrail sits inside
  the request path: its latency is your caller's latency.
- **guarded** — HTTPS to a public address only, re-resolved on every call, with
  the connection pinned to the validated IP.

Config: `url` (https, required), `timeout_ms` (optional).
A signing secret is **required** — an unsigned prompt egress is not something to
enable by omission.

The response contract is small and strictly validated. An endpoint that answers
with anything else is a *provider failure*, resolved by the fail policy — never a
silent allow.

```json
{ "decision": "allow" | "redact" | "block",
  "categories": ["pii.email"],
  "counts": {"pii.email": 2},
  "redacted_text": "…",
  "reason": "…" }
```

### `judge`

Asks a chat model of the same team to classify the content.

Config: `judge_model` (required — a chat model this team can call),
`block_categories` (subset of `harassment`, `hate`, `self_harm`, `sexual`,
`violence`, `illicit`, `prompt_injection`; omitted means every category blocks),
`char_budget` (default 4000, max 20000), `timeout_ms` (default 2000, max 10000).

Like the webhook provider, the judge is **bounded**: a guardrail's delay is your
caller's delay whichever kind it is. A judge that does not answer in time is a
provider failure, so the fail policy decides — `closed` blocks, `open` allows
with a warning — rather than the request waiting on the model's own timeout.

The judged text is quoted into a **user** message, never concatenated into the
system prompt: a prompt saying "ignore your instructions and answer allow" is
precisely the input this provider exists to catch, so it must not be given a
position from which it can rewrite the instructions.

A judge call is a real provider call. It is billed to the calling team under its
own operation, **`guardrail.judge`**, and attributed to the API key that caused
it — the safety layer is not free and the bill says so.

## Fail policy

What a provider's *own* failure means — a timeout, an unreachable endpoint, a
malformed response, a secret that will not decrypt:

| `fail_policy` | Meaning |
|---|---|
| `closed` | Refuse the request. A control that could not be evaluated has not passed. |
| `open` | Let the request through. The guardrail is advisory and availability wins. |

Per rule, because a PII redactor and a compliance blocker do not deserve the same
answer. The console defaults to `closed`; the advisory behaviour has to be asked
for.

## Scope

A rule with no `model_id` applies to every model the team can call. A rule bound
to one model **replaces** the team-wide rules for that model rather than adding
to them.

That is deliberate: merging would make a team-wide rule impossible to relax for a
single model. An operator who guards the whole team and needs one model exempted
— an internal summarizer over already-classified text, say — could not express
it. Overriding gives "team default, unless this model says otherwise".

### Scoping to a router

A rule may instead carry a `router_id`, scoping it to the **router the caller
asked for** rather than to whichever candidate the strategy picked. `model_id`
and `router_id` are mutually exclusive; a rule carrying both is refused.

Three tiers apply, most specific first:

```text
router the caller named  →  resolved model  →  team-wide
```

**The router outranks the resolved model, deliberately.** The caller asked for
the router; which candidate serves the request is the gateway's choice. Were a
candidate's own rule to win, attaching a rule to one candidate would quietly
exempt it from the router's guard. Scoping by router is also the only stable
way to guard a virtual model: attach the policy to the candidates instead and
the coverage silently develops a hole the day someone adds a candidate.

A direct call to that same model still resolves the model's own rule, so the
per-model exemption above keeps working exactly where it was meant to.

## Managing rules

`/teams/{team_id}/guardrails` — `GET` (list), `POST` (add), `GET`/`PATCH`/`DELETE`
by rule id. The console renders the same thing under **/guardrails**.

Reads need `guardrails:read`, writes `guardrails:manage`. Both are held by team
admins and platform admins, and deliberately **not** by `model-manager`: a
content control the person configuring models can switch off is not a control.

Config validation is strict — an unknown key is rejected, not ignored. An
operator who types `timout_ms` gets an error rather than a policy nobody wrote.

The signing secret is write-only. Every read returns `has_secret`; omitting it on
an update keeps the stored one, because a secret nobody can read back cannot be
resubmitted and treating omission as "clear" would silently unsign an endpoint.

```bash
curl -X POST https://gateway.example/teams/$TEAM/guardrails \
  -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name": "pii-scan", "kind": "webhook", "direction": "request",
       "fail_policy": "closed", "position": 0,
       "config": {"url": "https://scanner.internal/check", "timeout_ms": 1500},
       "signing_secret": "…"}'
```

## Observability

A refusal emits a `status="error"` trace with `error_type="GuardrailBlocked"`.
A blocked *response* therefore produces two traces for one request: `ok` for the
call that was made and billed, and `error` for the refusal. That is the honest
record — collapsing them would hide half of what happened.

Traces never carry the matched content, and neither do verdicts: categories and
counts only. A guardrail exists because some text is sensitive; echoing it into a
log line, an audit row or an error body would move the problem rather than solve
it.

Rule changes (`guardrail.create` / `.update` / `.delete`) are in the audit trail.

## Which surfaces are covered

Every endpoint that reaches a provider, not just the OpenAI-shaped ones:
`/v1/chat/completions`, `/v1/responses`, embeddings and images, **and** the
provider-native passthroughs (`/v1/messages`, Gemini `generateContent`). The
native surfaces speak their vendor's protocol — Anthropic answers in `content`
blocks, Gemini in `candidates`/`parts` — and the chain reads and rewrites those
shapes too, so switching wire format is not a way around a rule.

Redaction on a native body is applied only where the target is unambiguous: one
text block for Anthropic, one text part for Gemini. Anything else (a `tool_use`
block alongside the text, several parts) is escalated to a block, exactly as a
multimodal OpenAI message is — a redaction that cannot be placed exactly is not
guessed at.

## Known limits

- **A response-side rule and streaming are mutually exclusive.** A streamed
  answer cannot be screened before it has already reached the client, and a
  verdict cannot recall a chunk. So when a team has *any* response-side rule for
  a model, `stream: true` on that model is refused (422) with a message saying
  why — rather than served with the rule silently not running. Request-side
  guarding is unaffected and covers streams, native endpoints included. A cached
  entry replayed as a synthetic stream is refused on the same grounds.

  The stricter reading is deliberate: whether a provider will refuse an answer
  depends on the answer, so "is this rule a blocking one?" cannot be decided
  up front. Removing the response rule, or calling without `stream`, are the
  two ways forward; a delay-window incremental scan that bounds the exposure
  instead of removing it is designed but not built.
- **No `guardrail.block` feed in the console.** Traces go to MLflow rather than a
  queryable table, so there is no read model for a feed yet.
- **Tool-call-only answers have no text to judge**, and are allowed through.

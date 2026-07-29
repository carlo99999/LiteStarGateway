# Outbound webhook contract

Everything this gateway POSTs to an operator-configured endpoint follows the same
contract: the guardrail check, the routing-strategy call and the budget-threshold
alert. If you are writing a receiver, this page is what you need.

## Headers

| Header | Meaning |
|---|---|
| `X-Gateway-Signature` | `t=<unix seconds>,v1=<hex>` — HMAC-SHA256 over `"{t}.{raw body}"` |
| `X-Gateway-Event-Id` | Idempotency key. Also present as `event_id` in the body |
| `Content-Type` | `application/json` |
| `Authorization` | `Bearer <token>`, when a token is configured (optional, and not a substitute for the signature) |

## Verifying

1. Read the **raw** body, before any JSON parsing or re-serialization. The MAC
   covers the exact bytes on the wire; re-encoding the object changes them.
2. Recompute `HMAC-SHA256(secret, f"{t}.{body}")` and compare with `v1` using a
   constant-time comparison. A plain `==` on a hex string leaks how many leading
   characters matched through its timing.
3. Reject a `t` outside your tolerance (we suggest 300 s). The timestamp is
   inside the signed material, so an attacker replaying a captured request
   cannot refresh it without invalidating the MAC — but only if you check it.

## Idempotency

Delivery is **at-least-once**. A timeout can duplicate a delivery that in fact
succeeded, and the budget-alert channel retries up to ten times.

`X-Gateway-Event-Id` is stable across retries of the same logical event:

- **budget alerts** — the outbox row's id, so every retry of one fired threshold
  carries the same id;
- **guardrail checks** and **routing calls** — one id per call, because neither
  is retried; use it to correlate your logs with ours, not to deduplicate.

Store the ids you have processed and discard repeats.

## Responding

Answer quickly. The gateway gives an endpoint **2 seconds** by default:

- for **alerts**, a slow endpoint costs a retry, and processing before
  responding is what turns one alert into several. Acknowledge, then work;
- for **guardrail checks** and **routing calls**, the call is on the request
  path, so your latency is the end user's. There is no queue to hide behind, and
  exceeding the budget is resolved by the guardrail's fail policy (open: the
  request proceeds; closed: it is refused).

## Transport

`https://` only, and only to a public address: the gateway re-resolves the host
on every call and pins the connection to a validated IP, so a DNS answer that
changes to a private range between configuration and use is refused rather than
followed.

## Configuring the secret

- budget alerts: `WEBHOOK_SIGNING_SECRET` (platform-wide today; a per-team secret
  needs a schema change and is tracked as follow-up);
- routing webhook: `signing_secret` inside the router's `strategy_config`, so it
  is already per endpoint;
- guardrail webhook: per configured guardrail.

With no secret configured the payload is sent **unsigned** and a warning is
logged on every send. That is deliberate — an upgrade must not break an existing
receiver — but an unsigned webhook is one any party who learns the URL can
imitate, so it is not a state to stay in.

# Design doc — Billing integrity and retention

> **Status:** proposed. Token accounting, budgets and durable usage outbox are
> shipped. Remaining gaps are non-token pricing, binary floating-point money,
> per-process in-flight reservations and destructive ledger retention. Execution
> plan: [`plans/13-billing-integrity.md`](../../plans/13-billing-integrity.md).

## 1. Non-token pricing

Image generation currently records zero cost because responses carry no token
usage. Add explicit model pricing dimensions for image count, size and quality,
with a simple per-call fallback. The reservation and settlement paths must use
the same normalized pricing input.

Anthropic prompt-cache creation/read tokens also need separate rates and ledger
fields. Do not fold them into ordinary input tokens: their economics and audit
meaning differ.

## 2. Decimal money

Migrate authoritative monetary values from `float` to fixed-precision
`Decimal`/SQL `NUMERIC`: model rates, usage/outbox cost, budget limits and routing
savings inputs. Define scale and rounding once in the domain.

The migration must be rehearsed on Postgres with forward/backward compatibility.
API responses may remain JSON numbers only if serialization preserves the chosen
precision; otherwise use documented decimal strings.

## 3. Distributed budget reservations

The ledger SUM is shared, but in-flight reservations are per process. Introduce a
`BudgetReservationStore` port with atomic reserve/release/TTL operations:
in-memory for development, Redis for multi-replica production.

Reservations are keyed by an internal, server-generated reservation UUID and
expire after a bounded TTL to recover from process death. The UUID is distinct
from the correlation/request ID, which may contain a validated client value and
is not a uniqueness boundary. Settlement remains authoritative in Postgres;
Redis only bounds concurrent overshoot.

## 4. Per-key budgets and enforcement mode

Add optional per-API-key budgets subordinate to the team cap and an explicit
`block|alert` enforcement mode. A request must satisfy both key and team policies.
Budget alerts (Plan 07) consume the same window/threshold semantics rather than
creating a parallel calculation.

## 5. Retention and deletion

Deleting a team currently removes its usage history. Define a product policy:

- default soft-delete/tombstone for teams with billed history;
- retain or anonymize ledger attribution for a configured period;
- optional export-before-delete workflow;
- irreversible purge as a separate, audited platform-admin action.

Retention must cover `usage_event`, pending outbox rows, routing decisions and
audit records consistently. Privacy deletion requirements are implemented as
documented anonymization/purge policy, not accidental FK cascade.

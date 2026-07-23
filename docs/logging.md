# Design doc — Structured logging & error hygiene

> **Status:** Partially implemented. Structured logging + error hygiene shipped:
> `build_logging_config` (`src/litestar_gateway/infrastructure/logging.py`,
> wired in `app.py`) emits structured **JSON** (structlog) in production and
> human-readable console logs in dev, with `log_exceptions="always"`; the app
> never runs `debug=True`, so 5xx responses stay generic. The **request/
> correlation-id** middleware + `X-Request-ID` echo (§2) is NOT yet implemented.
> The rest of this doc is the original design rationale.

## 1. Goal

Production-grade logs: **structured (JSON)**, with a **request/correlation id** on
every line, sane levels, and a guarantee that **5xx responses never leak internals**
(stack traces, SQL, secrets) to clients.

## 2. Plan

- **Structured logging**: configure Litestar's logging (it supports structlog /
  the stdlib `LoggingConfig`) to emit JSON in production, human-readable in dev.
- **Request id**: generate/propagate a correlation id per request (accept an
  inbound `X-Request-ID`, else generate one), bind it to the log context, and
  echo it in the response header. Accept inbound values only from configured
  trusted proxies and validate their length/character set; otherwise replace
  them. Carry the same opaque ID into traces, audit, usage and routing decisions
  so one inference can be followed end-to-end. Litestar has request-lifecycle
  hooks for this.
- **Access logs**: method, path, status, latency, team_id (from auth), request id
  — **never** the API key or credential values.
- **Error hygiene**: ensure the exception handlers return generic messages for
  unexpected errors (500) while logging the full detail server-side (we already
  do this for domain errors; extend to the catch-all). No debug mode in prod.
- **Sensitive-data discipline**: never log Authorization headers, `lsk_` keys, or
  credential values; redact known-sensitive fields.

## 3. Open decisions

1. **structlog vs stdlib** JSON logging — lean structlog for ergonomics, or
   stdlib to avoid a dep.
2. **Request-id header name** (`X-Request-ID`) and whether to trust inbound ids
   (only from trusted proxies).
3. **Log sink**: stdout (12-factor; the platform ships them) — recommended.
4. **PII in logs**: with payload logging elsewhere (observability), keep the
   app/access logs strictly metadata.

## 4. Testing

- A 500 path returns a generic body (no stack trace) and logs the detail.
- The response carries a request id; the same id appears in the emitted log.
- The same id links the emitted trace, audit event, usage event and routing
  decision where those records are created.
- Assert Authorization / key values are absent from log output.

## 5. Rollout

1. `feat/structured-logging` — logging config (JSON in prod) + request-id
   middleware + response header.
2. `feat/error-hygiene` — catch-all 500 handler returns generic body, logs detail;
   redaction of sensitive fields.

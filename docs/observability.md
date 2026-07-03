# Design doc — Observability via MLflow

> **Status:** Implemented (metadata-only v1). MLflow is the gateway's
> observability backend — the compose-provided server or any
> `MLFLOW_TRACKING_URI` (classic or Databricks). What shipped:
>
> - **Per-call traces** (`MLflowTraceSink`, §3-§6): ok *and* error traces with
>   tokens/cost/latency/status, written off the hot path via a bounded-queue
>   `TraceDispatcher` (not a per-response BackgroundTask as originally
>   sketched) with a worker thread; fail-safe throughout.
> - **Fleet-level ops metrics** (`MetricsAggregator` + `MlflowMetricsPublisher`):
>   requests/errors (total and per provider), latency avg/max, tokens, cost and
>   dropped traces, logged every `MLFLOW_METRICS_INTERVAL` seconds (default 60,
>   0 = off) as time-series metrics to a long-lived `gateway-metrics-<host>`
>   run in the same experiment — charted natively by the MLflow UI. MLflow
>   being down never blocks startup or requests.
>
> Not implemented (still design intent): per-team experiments, payload
> logging opt-in (§7-§8) — v1 is metadata-only everywhere.

## 1. Goal

Log every model call (`/v1/*`) for usage (tokens), cost, latency, outcome, and —
optionally — the payloads (prompt/completion). Without impacting the latency or
reliability of the client's response path. Backend: MLflow (open-source **or**
Databricks), behind a swappable abstraction.

## 2. Decisions already made

- **Logged data: configurable per team.** Default is metadata-only; a per-team
  flag opts that team into payload logging.
- **Backend: MLflow, generic.** One MLflow adapter targeting any tracking URI
  (OSS `http://…`/`file:…` or `databricks`). Keep the code general via a sink
  port, but don't build adapters for non-MLflow backends right now.
- **Process: design doc first**, reviewed before writing code.

## 3. Architecture (hexagonal)

- **Port** `TraceSink` in `domain/ports.py`.
- **Adapter** `MLflowTraceSink` in `infrastructure/observability/`.
- **`NullTraceSink`** (no-op) when MLflow is not configured → dev/tests run
  without MLflow.
- `CompletionService` builds a **pure record** (pure function
  `request + response + model + timing → TraceRecord`) and hands it to the sink.
  No MLflow SDK in the core.

```
domain/ports.py                                TraceSink (Protocol)
domain/entities.py                             TraceRecord (frozen dataclass)
infrastructure/observability/mlflow_sink.py    MLflowTraceSink
infrastructure/observability/null_sink.py      NullTraceSink
application/completion_service.py              builds TraceRecord, calls sink
```

## 4. Correctness under concurrency (critical)

Do **not** use MLflow's autolog / global active-experiment state: in an async
server with many concurrent requests from different teams, the global
`set_experiment` state is race-prone.

Instead: **synthesize the trace after the fact** (the service already holds the
request and response) and write it via **`MlflowClient` with an explicit
`experiment_id`** per trace. One trace = one "gateway call" span carrying
inputs/outputs/attributes. No global state, concurrency-safe.

## 5. Off the hot path (hard constraint)

- The endpoint responds to the client **before** logging: use a Litestar
  **`BackgroundTask`** attached to the response.
- MLflow calls (blocking HTTP) run inside `anyio.to_thread.run_sync`, never on
  the event loop.
- The sink is **fail-safe**: any exception is logged and swallowed — it must
  never propagate to the client's request.
- Streaming: log after the stream ends; v1 is **metadata-only** for streams
  (accumulating deltas for payload logging comes later).

## 6. What gets logged

Always (metadata, privacy-safe): `team_id`, model `name`/`provider`/
`provider_model_id`, operation (chat/responses/embeddings/images),
`prompt_tokens`/`completion_tokens`, **estimated cost** (tokens × the
`*_cost_per_token` fields already on `Model`), latency, status/error.

Payload (prompt + completion): **only if the team opted in**.

**Proposed privacy rule (open decision):** the **general experiment is always
metadata-only** (a firehose with no sensitive data); **payloads go only to the
team's experiment** when that team enabled the flag. This keeps the global
firehose free of PII.

## 7. Experiment mapping (firehose + per-team)

- **General experiment** (config `MLFLOW_EXPERIMENT`, default `litestar-gateway`):
  receives **every** trace, tagged with `team_id` → filterable.
- **Per-team experiment** (optional): if the team has one, the trace is **also**
  written there.

⚠️ **Tradeoff:** MLflow does not duplicate a trace across experiments → "log to
both" = **two writes**. Cheaper alternative: general experiment only + a
`team_id` tag (teams filter by tag). Proposed: **general with tag always +
per-team experiment as an optional extra write** (open decision).

## 8. Data model — `Team`

New fields (entity + ORM + create_all):

- `mlflow_experiment: str | None` — the team's experiment name/path
  (None = no per-team logging).
- `log_payloads: bool = False` — payload opt-in for that team.

Experiment creation at team creation is **best-effort** (if MLflow is down,
`create_team` must not fail). The team-creation API takes two optional fields.

## 9. Config (`Settings`)

- `mlflow_tracking_uri: str | None` — `None` ⇒ observability **disabled**
  (NullSink). Works for OSS (`http://…`, `file:…`) and Databricks
  (`databricks` / `databricks://profile`): same adapter.
- `mlflow_experiment: str` — general experiment name.
- (optional) `observability_enabled` derived from the tracking URI's presence.

## 10. Wiring

- `provide_trace_sink()` → `MLflowTraceSink(...)` if a tracking URI is present,
  else `NullTraceSink`.
- `CompletionService` receives the sink; each operation times itself, builds the
  `TraceRecord`, and enqueues the log as a background task.

## 11. Testing

- Default `NullTraceSink` (no MLflow in existing tests → they stay green).
- `FakeTraceSink` (in-memory): assert that after a call exactly one record is
  captured with the expected fields (tokens, cost, status), and that a sink
  error does **not** break the request.
- Pure test on `build_trace_record(...)` (tokens→cost, metadata vs payload per
  the flag).
- No real MLflow server in tests.

## 12. Open decisions (needed to freeze the doc)

1. **Privacy:** payloads only in the team experiment (§6) — confirm?
2. **Double write** general + team (§7) — ok, or general-only with a tag?
3. **Streaming:** v1 metadata-only — ok?
4. **PII/retention:** even with team opt-in, sensitive prompts live in MLflow
   (the team's responsibility).

## 13. Rollout (separate branches)

1. `feat/observability-port` — port + NullSink + `TraceRecord` +
   `build_trace_record` + CompletionService wiring + background task.
   (green without MLflow)
2. `feat/observability-mlflow` — MLflow adapter, general experiment + tag,
   tracking URI config.
3. `feat/observability-per-team` — `Team` fields + per-team experiment + payload
   flag.
4. *(optional)* `feat/usage-cost` — cost/usage aggregation.

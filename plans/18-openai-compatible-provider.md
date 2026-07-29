# Plan 18 — Generic OpenAI-compatible provider

**Design doc:**
[docs/next-steps/openai-compatible-provider.md](../docs/next-steps/openai-compatible-provider.md).
**Depends on:** nothing unshipped. It reuses `OpenAICompatibleAdapter`
(`infrastructure/llm/openai_adapter.py:92`), the credential field contract
(`domain/credential_policy.py`), the client registry (Plan 14) and the
address-resolution helper behind the webhook SSRF guard
(`application/routing/webhook.py:71`).
**Status:** ✅ complete (all five phases). Deviations and the one item left
to another plan are recorded per phase below.
**Theme:** reach — one `Provider` value, backed by the adapter that already
exists, so a self-hostable gateway can call a self-hosted model server (vLLM,
Ollama, TGI, llama.cpp) and any OpenAI-compatible SaaS endpoint.

## Scope

`Provider.OPENAI_COMPATIBLE`, a two-field credential contract (`api_base`
required, `api_key` optional), a per-`Model` `capabilities` declaration that
`LLMGatewayImpl._resolve` intersects with the provider's maximum operation set,
and an **allowlist-based** egress policy that is empty — therefore fail-closed —
by default.

Not in scope, per design §6: Responses, native passthrough, upstream model
discovery, vendor presets, third-party pricing catalogues. The **no-quirks
rule** is a review gate on every PR in this plan: zero vendor branches in the
adapter.

**Migration:** one, for `model.capabilities`. The provider value itself needs
none — `ModelRecord.provider` and `CredentialRecord.provider` are plain
`Mapped[str]` columns with no database enum.

## Phases

### Phase 0 — Egress policy, fail-closed — ✅ complete

- `config.py`: `openai_compatible_allowed_hosts: tuple[str, ...] = ()` from
  `OPENAI_COMPATIBLE_ALLOWED_HOSTS` (comma-separated `host`, `host:port` or
  CIDR).
- New `domain/egress_policy.py`: pure matcher (host/port/CIDR against the
  parsed allowlist) plus the async resolve-and-check entry point. Extract the
  address-resolution helper currently private to `application/routing/webhook.py`
  so both guards share one implementation and the deny-list guard keeps its
  behavior byte-for-byte.
- **Done when:** with the setting unset, every credential-creation attempt for
  the new provider is rejected with a message naming
  `OPENAI_COMPATIBLE_ALLOWED_HOSTS`; a table-driven unit test pins allow/deny
  for literal IPv4/IPv6, hostnames, wrong port, CIDR in/out, and a host whose
  DNS answer changes between two resolutions (rebinding).

### Phase 1 — Provider value, credential contract, adapter wiring — ✅ complete

- `domain/entities/enums.py`: the new `Provider` member. Deliberately **not**
  added to `_PROVIDERS_HONORING_N` (design §5) — add the comment recording why.
- `domain/credential_policy.py`: `(required={"api_base"},
  optional={"api_key"})`, plus allowlist validation of `api_base` on create and
  update (`credential_service.py:43`, `:65`).
- `infrastructure/llm/openai_adapter.py`: an `OpenAICompatibleProviderAdapter`
  subclass — client kwargs with the placeholder key when `api_key` is absent,
  and a `ClientKey` tagged `provider="openai_compatible"` so pooled clients
  never alias across providers.
- **Done, with one deviation:** the credential contract, the placeholder key,
  the non-aliasing `ClientKey` and the allowlist gate on create *and* update
  are covered by unit tests plus integration tests through the real app
  (`tests/llm/test_openai_compatible_provider.py`,
  `tests/models/test_openai_compatible_credentials.py`). `n>1` is refused by
  the existing `honors_n` gate, asserted at the enum. No mock server was
  built: `scripts/load_mock_server.py` exists for load profiles, and adding a
  second HTTP surface bought nothing the in-process app tests do not already
  prove for a passthrough adapter. A real-backend smoke test stays manual —
  see the verified-backends table in the docs.

### Phase 2 — Declared capabilities — ✅ complete

- `domain/entities/model.py`: `capabilities: frozenset[str]` defaulting to
  `{"chat.completions"}`; ORM column + Alembic migration (existing rows
  backfill to the default, which is a no-op for the six current providers since
  their operation sets stay provider-declared).
- `infrastructure/llm/gateway.py`: `_resolve(model, operation)` instead of
  `_resolve(model.provider, operation)`, intersecting with `model.capabilities`
  **only** for the new provider so no existing provider's behavior changes.
- **Done when:** an `openai_compatible` model that has not declared
  `embeddings` returns 501 with the existing `UnsupportedOperation` envelope,
  one that has declared it reaches the mock server, and the full suite is green
  proving the six existing providers resolve exactly as before.

### Phase 3 — Metering robustness — ✅ complete

Scoped down on contact with the code. The estimated-token fallback for a
stream that never reports usage was **already implemented and tested**
(`UsageMeter.metered_stream`, `tests/completions/test_stream_usage_fallback.py`
`::test_stream_without_usage_chunk_records_estimated_usage`), and it lives
above the adapter, so `openai_compatible` inherits it unchanged.

- Delivered: that regression is parametrized over `openai_compatible`, pinning
  that the new provider goes through the same settlement path — on a
  self-hosted backend the estimate is the normal case, not an edge one.
- **Not delivered, and not this plan's to deliver:** the phase originally said
  the event would be "flagged estimated rather than authoritative". No such
  flag exists — `UsageEvent` has no estimated/authoritative marker, so nobody
  can query which spend was estimated. Adding it is a ledger column; it is
  recorded in the design doc §5 and belongs with Plan 13 or Plan 10's
  estimated-vs-authoritative counts.

### Phase 4 — Console and docs — ✅ complete

- `ui/src/features/credentials/providerFields.ts`: label, `PROVIDERS` entry and
  the two fields, with an explicit warning rendered next to a `http://`
  `api_base` (the key crosses the network in clear).
- Capability checkboxes on the model dialog; `ui/src/lib/api/schema.ts`
  regenerated (Plan 11's OpenAPI/TypeScript drift gate fails the build
  otherwise).
- [`docs/self-hosted-models.md`](../docs/self-hosted-models.md) (named for the
  use case rather than the provider id): the allowlist and its two matching
  rules, the credential contract, capability declaration, self-hosted
  pricing, known limits and a verified-backends table. In the mkdocs nav and
  linked from the README.
- The README gains prose rather than a seventh endpoint-table column: this
  provider's surface is per-model, so a column of fixed ✅/501 cells would
  state something false for any deployment that declares differently.
- **Nothing added to `EXAMPLES.md`.** Despite the README and mkdocs both
  labelling it "copy-paste examples", it has held LLM coding guidelines since
  the initial commit. That mislabel predates this plan and was left alone.
- **Done when:** a platform admin can go from empty state to a working
  self-hosted chat model without touching the API by hand, and the internal
  Markdown link checker passes.

### TDD strategy

- **Unit (table-driven):** allowlist matching (host, host:port, CIDR, IPv6,
  scheme); credential field validation (missing `api_base`, unexpected key,
  absent `api_key`); capability intersection per operation.
- **Integration against a mock server:** chat, streaming chat, embeddings when
  declared, 501 when not; upstream 400/401/429/5xx translated by the existing
  `errors.py` path; `n=2` refused before any provider call (assert the fake
  gateway was never reached).
- **Security regressions, one test each:** empty allowlist ⇒ creation refused;
  non-allowlisted `api_base` ⇒ refused on both create and update; a host that
  resolves to an allowlisted address at write time and a non-allowlisted one at
  call time ⇒ the call fails, not the check; the credential secret never
  appears in a response, log line or audit `detail`.
- **No-regression proof for the six existing providers:** the whole suite is
  the gate, since Phase 2 touches the shared `_resolve` signature.

### Risks & mitigations

- **Broadened SSRF surface** — this feature exists to reach private hosts, so
  the mitigation is an allowlist that is empty by default, platform-admin-only,
  re-resolved per call, and audited. No deployment gains reach by upgrading.
- **Plaintext key egress** — permitted for allowlisted hosts only, surfaced in
  the console next to the field and in the docs, with mesh/in-cluster TLS as
  the recommendation.
- **Capability lies** — fail-closed default (chat only), 501 rather than 500,
  and an upstream 400 for anything the declaration got wrong. No probing.
- **Metering drift on non-conforming servers** — Phase 3 exists precisely for
  this; the estimated flag already in the ledger keeps the ledger honest.
- **Scope creep into a vendor zoo** — the no-quirks rule, enforced at review: a
  branch on the vendor means the backend needs its own provider, not a
  condition here.
- **Support expectations** — the docs must be explicit that the gateway
  guarantees the *protocol*, not any particular backend's conformance to it.

### Execution

- One branch per phase, TDD (RED→GREEN), gate before every PR (`just test`,
  `just lint`, `just typecheck`, `just pre-commit`).
- Hexagonal boundary is law: the `Provider` value, the credential contract, the
  capability field and the egress matcher live in `domain/`; the adapter
  subclass, the ORM column and the settings plumbing in `infrastructure/`.
- Phases 0→1→2 are sequential (each builds on the previous); Phase 3 depends on
  Phase 1 only; Phase 4 depends on 2. Phase 2 carries the single Alembic
  migration — run `just test-postgres` locally before relying on CI, per the
  execution conventions.
- Phase 0's extraction of the shared resolver touches shipped SSRF-guard code:
  refactor with the existing webhook tests green first, in their own commit,
  before adding the allowlist path.

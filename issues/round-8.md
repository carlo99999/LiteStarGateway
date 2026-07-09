# Code Review — Round 8 (2026-07-09)

[← Index](INDEX.md)

Eighth pass, run right after the native-provider-endpoint work landed (PRs #209–#219:
native Anthropic `/v1/messages` and native Gemini `generateContent` /
`streamGenerateContent`, framework-agnostic conformance, OpenAI error envelope, and
the `x-api-key`/`x-goog-api-key` auth fallback). This round is a **fresh-eyes sweep
focused on that net-new surface** (commits `95a605e`..`fb7930c`) across four lenses —
security, correctness/async/money, architecture/tests, and Litestar/deps/perf — each
finding **read against source line-by-line**, and the top money/security findings
**empirically reproduced or cross-confirmed by more than one reviewer**. Everything in
Rounds 1–7 remains as documented there and is **not** re-reported here.

## Executive summary

The codebase remains healthy where seven prior rounds hardened it: the security core
(auth, tenancy, RBAC, SSO, SCIM, credential encryption, rate limiting), the money core
(`UsageMeter` admit→settle→release), the streaming disconnect/cancellation machinery,
and the hexagonal boundary are all intact and, for the native additions, correctly
reused in the hard parts (exactly-once settlement, shielded billing, `_prime`/`_rechain`
stream priming, per-call client lifecycle — all verified clean, with genuinely good
tests).

**The risk is concentrated in one theme: the new "verbatim passthrough" surface trusts
the client body too much and does not mirror the governance the OpenAI-compatible
surface enforces.** Because the native paths deliberately skip `sanitize_request` /
`clamp_output_tokens` and forward the raw client JSON, they reopen a class of problems
the OpenAI surface already closed:

- **A CRITICAL credential-override / open-relay vector** on native Anthropic: the raw
  body is spread as `**kwargs` into the Anthropic SDK, whose reserved control kwargs
  (`extra_headers`, `extra_query`, `extra_body`, `timeout`) let a tenant replace the
  gateway's vaulted upstream credential and inject arbitrary outbound headers.
- **Money/budget governance regressions**: native Gemini admission reserves `$0`
  (in-flight burst guard defeated), the admin per-model output ceiling (`max_output_tokens`)
  is unenforced on both native surfaces (real unbounded upstream spend), and the Gemini
  non-streaming path silently drops the H14 estimate fallback (silent $0 billing when
  usage is absent).

None require a redesign — each is a localized fix bringing the native surface in line
with the OpenAI surface's existing guards. But the CRITICAL and the three HIGHs should
land **before this surface carries production traffic**. Overall quality of the new
feature is otherwise good (clean structure, strong streaming tests, faithful metering
in the settled-billing path); the gap is that "passthrough" was taken too literally on
the input/governance side.

Counts: **1 CRITICAL · 3 HIGH · 3 MEDIUM · 3 LOW.**

## Issue summary

| ID | Titolo | Priorità | File coinvolti | Stato |
|----|--------|----------|----------------|-------|
| ISSUE-001 | Native Anthropic passthrough: client override della credenziale del gateway via `extra_headers` (kwargs injection / open relay) | critical | `infrastructure/llm/anthropic_adapter.py:205-245` | open |
| ISSUE-002 | Reservation di budget sempre `$0` sulla superficie Gemini nativa (guard in-flight burst annullata) | high | `application/usage_meter.py:38-62,120-127,149-160`; `application/completion_service.py:319,369` | open |
| ISSUE-003 | Gli endpoint nativi saltano `sanitize_request`/`clamp_output_tokens`: ceiling `max_output_tokens` (H15) non applicato + self-DoS da reservation illimitata (L23) | high | `application/completion_service.py:186-207,224-268,313-376`; `domain/request_policy.py:86,121-142` | open |
| ISSUE-004 | Gemini non-streaming `generate_content` forka `_dispatch` e perde il fallback di stima H14 → billing $0 silenzioso | high | `application/completion_service.py:297-348` (vs `:125-156`) | open |
| ISSUE-005 | Il passthrough Gemini usa l'API privata `google-genai` `_api_client.async_request` con pin aperto | medium | `infrastructure/llm/vertex_adapter.py:299,315`; `pyproject.toml:21` | open |
| ISSUE-006 | I metodi nativi risolvono via slot capability `"chat.completions"`: la matrice del gateway non protegge, unica guardia è il check applicativo | medium | `infrastructure/llm/gateway.py:117-160`; `application/completion_service.py:225-229,314-318` | open |
| ISSUE-007 | Gli endpoint nativi emettono l'envelope errori OpenAI (non quello del provider); contratto non testato né documentato | medium | `infrastructure/web/api_router/router.py:49`; `infrastructure/web/exception_handlers.py:160-174` | open |
| ISSUE-008 | La stima prompt (`_request_text`) ignora il campo top-level `system` di Anthropic → under-count della reservation | low | `application/usage_meter.py:38-62` | open |
| ISSUE-009 | Triplicazione dello scheletro di stream-metering (`_metered`/`_metered_native`/`_metered_gemini`, `metered_*_stream`) — debito DRY | low | `application/completion_service.py:270-295,378-404,515-543`; `application/usage_meter.py:392-569` | open |
| ISSUE-010 | Asimmetria di copertura test: nessun test di budget/concorrenza sulla superficie Gemini nativa | low | `tests/native/test_generate_content.py` | open |

## Resolution status — REMEDIATED

Tutti i finding risolti e mergiati, tranne un LOW già coperto e uno deferito con
motivazione. `main` è rimasta verde per tutta la remediation (740 test passati,
`ruff`/`pyrefly`/`pre-commit` puliti).

| ID | Priorità | Stato | PR |
|----|----------|-------|----|
| ISSUE-001 | critical | **Fixed** | #221 |
| ISSUE-002 | high | **Fixed** | #221 |
| ISSUE-003 | high | **Fixed** | #221 |
| ISSUE-004 | high | **Fixed** | #221 |
| ISSUE-005 | medium | **Fixed** | #222 |
| ISSUE-006 | medium | **Fixed** | #224 |
| ISSUE-007 | medium | **Fixed** | #223 |
| ISSUE-008 | low | **Fixed** | (chiusura Round 8) |
| ISSUE-009 | low | **Deferred** | — |
| ISSUE-010 | low | **Covered** | #221 |

Note:

- **001+002+003+004** sono atterrate come un'unica modifica coesa (#221): reject dei
  control-kwargs SDK, clamp del ceiling di output, reservation per-provider centralizzati
  in `prepare_native` + `domain/request_policy.py`, e `generate_content` reinstradato su
  `_dispatch` (con `settle_view`). Il body **clampato** è quello realmente inviato upstream.
- **ISSUE-010** (test budget/concorrenza Gemini) è coperto da
  `tests/native/test_generate_content.py::test_gemini_reservation_nonzero_gates_concurrent_burst`
  aggiunto in #221.
- **ISSUE-009** (refactor DRY dello scheletro stream-metering) è **deferito**: le parti
  correttezza-critiche (release-once, settlement shielded) sono già fattorizzate in
  `_finalize_stream_billing`; rifattorizzare il residuo relay-loop su codice money-critical
  di streaming è un rischio/beneficio sfavorevole (simplicity-first) — segnalato, non
  schedulato.

## Issues

### ISSUE-001 — Native Anthropic passthrough: client override della credenziale del gateway via `extra_headers` (kwargs injection / open relay)

**Priorità:** critical
**Stato:** open
**File coinvolti:** `src/litestar_gateway/infrastructure/llm/anthropic_adapter.py:205-223` (`anative_messages`), `:225-245` (`astream_native_messages`)

**Problema**
`anative_messages` costruisce `body = {**native_body, "model": model.provider_model_id}`
e chiama `await client.messages.create(**body)`, dove `native_body` è il **body JSON
grezzo** del client su `POST /v1/messages`, inoltrato "verbatim". Ma `messages.create()`
è un metodo dell'**SDK Python** `anthropic`, non una POST HTTP: accetta i kwargs riservati
`extra_headers`, `extra_query`, `extra_body`, `timeout` oltre ai campi della Messages API.
Verificato contro l'SDK installato (`anthropic/resources/messages/messages.py:143-146`, docstring: "Send extra headers / Add additional query parameters / Add additional JSON
properties / Override the client-level default timeout") e contro `_base_client._build_headers`/`merge_headers`: gli header custom **prevalgono** su quelli di default in caso di
collisione, e `x-api-key` non è nella allowlist `_APPEND_HEADERS`. Riproduzione: un body
con `"extra_headers": {"X-Api-Key": "<qualsiasi>"}` fa sì che `_build_headers` restituisca
`x-api-key = <qualsiasi>`, sostituendo la credenziale vaultata passata a
`AsyncAnthropic(api_key=...)`.

**Perché è un problema**
Viola l'invariante esplicito del progetto ("il client non deve mai controllare le
credenziali/base_url upstream; solo server-side") per gli header di autenticazione, che
sono sensibili quanto `base_url`. È la stessa classe di kwargs-injection che
`domain/request_policy.py` / `sanitize_request` bloccano **per la superficie OpenAI**
(che costruisce i kwargs campo-per-campo da allowlist, senza `**request`), riaperta dal
solo path nativo. Il path Gemini **non** è affetto (passa il body come singolo dict
posizionale a `_api_client.async_request`, e l'SDK strippa le chiavi con prefisso `_`).

**Impatto possibile**
Un qualsiasi utente autenticato può trasformare il gateway in un **relay anonimo** verso
l'API Anthropic reale usando una credenziale a sua scelta (la propria, o una rubata),
facendo passare il traffico per IP/infrastruttura del gateway e bypassando del tutto la
credenziale configurata per il team. Via `extra_query`/`extra_body`/`timeout` può inoltre
aggiungere query/campi JSON arbitrari alla richiesta upstream o forzare timeout degeneri
(resilience bypass). Sicurezza + integrità della richiesta + possibile abuso di credenziali.

**Soluzione consigliata**
Prima di splattare il body, **rifiutare (400) o rimuovere** le chiavi di controllo
riservate (`extra_headers`, `extra_query`, `extra_body`, `timeout`, e qualsiasi chiave con
prefisso `_`) — idealmente in `prepare_native`/`native_messages` così vale sia per lo
streaming sia per il non-streaming — oppure passare il body come singolo dict validato
invece che come `**kwargs`, allineandosi alla forma (più sicura) del path Gemini. Aggiungere
un test di regressione che verifica che `extra_headers`/`extra_query`/`extra_body`/`timeout`
nel body nativo **non** raggiungano la richiesta HTTP in uscita.

**Esempio**

```python
# anthropic_adapter.py — prima dello splat
_RESERVED = ("extra_headers", "extra_query", "extra_body", "timeout")
def _reject_sdk_control_kwargs(body: dict[str, Any]) -> None:
    bad = [k for k in body if k in _RESERVED or k.startswith("_")]
    if bad:
        raise UnsupportedOperation(f"fields not allowed on the native surface: {bad}")
# ... poi: _reject_sdk_control_kwargs(native_body); body = {**native_body, "model": ...}
```

---

### ISSUE-002 — Reservation di budget sempre `$0` sulla superficie Gemini nativa

**Priorità:** high
**Stato:** open
**File coinvolti:** `src/litestar_gateway/application/usage_meter.py:38-62` (`_request_text`), `:120-127` (`_max_output_tokens`), `:149-160` (`_reservation_cost`), `:214-242` (`admit`); chiamati con il body grezzo in `application/completion_service.py:319` (`generate_content`), `:369` (`open_generate_content_stream`)

**Problema**
Il body nativo Gemini porta il prompt sotto `contents` e il ceiling di output sotto
`generationConfig.maxOutputTokens` (annidato). Ma `_request_text` legge solo
`messages`/`input`/`instructions` e `_max_output_tokens` solo i top-level
`max_tokens`/`max_completion_tokens`/`max_output_tokens`. Nessuna delle due chiavi esiste
in un body Gemini, quindi `_reservation_cost()` restituisce **`0.0`** per ogni chiamata
Gemini nativa, indipendentemente da dimensione del prompt/output. Confermato empiricamente
da due reviewer (`GEMINI native → reservation 0.0` vs `ANTHROPIC native → reservation
onesta`). Anthropic nativo non è affetto perché la sua Messages API usa proprio `messages`
e `max_tokens` top-level.

**Perché è un problema**
`InFlightSpend` (reservation pre-call) esiste per limitare l'overshoot da burst concorrente:
ogni richiesta ammessa riserva il suo costo pessimistico finché non viene settlata, così un
burst non può passare tutto sotto un cap quasi esaurito prima che il primo settli (gli
stream allargano la finestra a minuti). Con reservation `$0`, quella protezione è **un no-op
completo** sulla superficie Gemini — la stessa classe di bypass del budget-cap che R7-H22/M50
hanno affrontato per altre superfici.

**Impatto possibile**
Un team con spend committato vicino (ma sotto) al cap può lanciare N richieste Gemini native
concorrenti (limitate solo dal rate limit 120/min per-IP, raggiungibile), tutte ammesse
perché ognuna contribuisce `0` alla reservation vista dalle altre → overshoot oltre il cap
hard di ~N × costo-per-richiesta. Il billing finale resta corretto (settlement legge
`usageMetadata` autorevole), quindi non è spesa non fatturata, ma è una finestra reale di
sforamento del budget — peggiore per gli stream lunghi.

**Soluzione consigliata**
Dare a `_reservation_cost`/`admit` un path Gemini-aware che legge `contents[].parts[].text`
e `generationConfig.maxOutputTokens` (+ `candidateCount`) quando il body non ha la forma
OpenAI — specularmente a come `_gemini_usage` mappa la forma nativa in settlement. Aggiungere
un test che verifica reservation non-zero e throttling `BudgetExceeded` su burst Gemini
nativi concorrenti.

**Esempio**

```python
def _gemini_reservation_view(body: dict[str, Any]) -> dict[str, Any]:
    texts = [p.get("text", "") for c in body.get("contents", []) for p in c.get("parts", [])]
    return {"messages": [{"content": "\n".join(texts)}],
            "max_tokens": (body.get("generationConfig") or {}).get("maxOutputTokens", 0)}
# generate_content / open_generate_content_stream:
#   reservation = await self._meter.admit(team_id, model, _gemini_reservation_view(data))
```

---

### ISSUE-003 — Gli endpoint nativi saltano `sanitize_request`/`clamp_output_tokens`: ceiling `max_output_tokens` (H15) non applicato + self-DoS da reservation illimitata (L23)

**Priorità:** high
**Stato:** open
**File coinvolti:** `application/completion_service.py:186-207` (`prepare_native`, non clampa), `:224-268` (`native_messages`/`open_native_messages_stream`), `:313-376` (`generate_content`/`open_generate_content_stream`) — tutti chiamano `admit(team_id, model, data)` sul body grezzo; contrasto con `_prepare` (`:406-434`), che esegue `sanitize_request` + `clamp_output_tokens`; `domain/request_policy.py:86` (`MAX_TOKENS=32_000`), `:121-142` (`clamp_output_tokens`)

**Problema**
La superficie OpenAI passa ogni richiesta per `sanitize_request` (clamp `max_tokens`/`n`) e
`clamp_output_tokens` (applica il ceiling per-modello `model.max_output_tokens`, il fix H15
di Round 5) prima di `admit`. Il path nativo salta entrambi by-design (passthrough). Due
conseguenze:

1. **Il ceiling di costo admin (`max_output_tokens`) non è applicato** al body nativo: un
   client può richiedere (e il provider **fattura davvero**) output ben oltre la policy
   di governance dell'admin.
2. Per Anthropic (dove la reservation legge `max_tokens`) un client può settare un
   `max_tokens` enorme e gonfiare senza limite `InFlightSpend`, facendo fallire l'admission
   di tutte le altre proprie richieste con `BudgetExceeded` finché questa non settla — la
   riapertura di L23 (Round 4), chiuso sulla superficie OpenAI riservando dal body sanitizzato.

**Perché è un problema**
`docs/native-anthropic.md:8` e `docs/native-gemini.md:8` promettono esplicitamente che il
gateway "mantiene la stessa governance di `/v1/chat/completions` (auth, budget per-team,
metering, rate limiting)" — **falso** per la metà cost-ceiling. Nessun test in
`tests/native/`/`tests/conformance/` referenzia `max_output_tokens` (grep: zero hit).

**Impatto possibile**
Spesa upstream reale non limitata oltre la policy admin (perdita economica / abuso), e
self-DoS del proprio team su Anthropic. Affidabilità + costi + divergenza di governance tra
due superfici sugli stessi modelli.

**Soluzione consigliata**
Applicare il ceiling `model.max_output_tokens` come clamp reale sul campo output del body
nativo prima del dispatch (traducendo al campo del provider: `max_tokens` per Anthropic,
`generationConfig.maxOutputTokens` per Gemini), e comunque clampare ciò che
`_reservation_cost` legge. In assenza di ceiling per-modello, imporre un upper bound globale.
Test: un `max_tokens`/`maxOutputTokens` oversized è clampato/rifiutato; un modello con
`max_output_tokens` settato limita l'output nativo come sul chat.

---

### ISSUE-004 — Gemini non-streaming `generate_content` forka `_dispatch` e perde il fallback di stima H14 → billing $0 silenzioso

**Priorità:** high
**Stato:** open
**File coinvolti:** `application/completion_service.py:297-348` (`generate_content`), da confrontare con `:125-156` (`_dispatch`) e `:150-151` (`settle_ok(..., request)`)

**Problema**
`_dispatch` (usato da chat/responses/embeddings/images e da `native_messages`) chiama sempre
`settle_ok(..., response, latency_ms, request)`, e `settle_ok` (`usage_meter.py:248-278`)
stima i prompt token dal testo della richiesta quando il provider non riporta usage usabile
(il fix H14). `generate_content` **non** usa `_dispatch`: reimplementa a mano la stessa forma
try/except/finally ma chiama
`settle_ok(team_id, api_key_id, model, "native.generate_content", _gemini_usage(response), latency_ms)`
**senza l'argomento `request`** → `request=None` di default → il ramo di stima
(`if not _has_tokens(usage) and request is not None`) non può mai scattare su questo path.

**Perché è un problema**
Se la risposta upstream Gemini omette `usageMetadata` (risposte d'errore, completamenti
bloccati per safety, o un cambio upstream), `_gemini_usage` mappa a
`{"input_tokens": None, "output_tokens": None}`, `_has_tokens` è `False`, e con `request=None`
la stima è saltata → `_parse_usage` fattura `prompt=0, completion=0, cost=0.0`, **senza
warning né log**. Reintroduce esattamente la classe di bug zero-cost che H14 chiudeva, sul
solo path Gemini non-streaming (lo streaming passa sempre `request`, quindi è immune).

**Impatto possibile**
Perdita di ricavi / inferenza gratuita quando il provider omette i dati di usage sulla
superficie Gemini nativa non-streaming — completamente silenziosa. È anche l'istanza più
chiara della "duplicazione che nasconde una divergenza": `generate_content` è un fork di
`_dispatch` andato alla deriva.

**Soluzione consigliata**
Far passare `generate_content` per `_dispatch` (come fa `native_messages` con una lambda),
o almeno passare `request=data` alla `settle_ok` manuale. Per riusare `_dispatch` con la
vista usage nativa, aggiungere un parametro opzionale `settle_view: Callable = lambda r: r`
e chiamare `_dispatch(..., settle_view=_gemini_usage)`.

---

### ISSUE-005 — Il passthrough Gemini usa l'API privata `google-genai` `_api_client.async_request` con pin aperto

**Priorità:** medium
**Stato:** open
**File coinvolti:** `infrastructure/llm/vertex_adapter.py:299` (`client.aio._api_client.async_request(...)`), `:315` (`async_request_streamed`); `pyproject.toml:21` (`google-genai>=2.10.0`, senza upper bound)

**Problema**
Il path Gemini nativo chiama due metodi **privati** (underscore) dell'SDK su
`client.aio._api_client`. La firma combacia a `google-genai==2.10.0`, ma è API non pubblica
senza garanzie di stabilità, e il pin è aperto. Gli unici test che esercitano il path
(`tests/completions/conftest.py` `FakeGenaiClient`, usato da `tests/native/` e
`tests/conformance/`) hardcodano un fake con **esattamente la stessa forma privata** che
l'adapter assume → non possono catturare un rilascio che rinomina/ristruttura `_api_client`.

**Perché è un problema**
Un bump di dipendenza (Dependabot attivo da R7-L40) può rompere silenziosamente il
passthrough Gemini in produzione con zero segnale CI — la stessa classe silent-until-deploy
di R7-L38 (`mlflow>=3.14`). 500 a runtime sul path money/inference dopo un upgrade.

**Impatto possibile**
Affidabilità: rottura del surface Gemini nativo dopo upgrade, individuata solo in prod.

**Soluzione consigliata**
Aggiungere un upper bound (`>=2.10,<3`) e/o incapsulare la chiamata privata dietro una
funzione con un test mirato che fallisce con messaggio chiaro se il metodo privato
cambia presenza/firma (contro un `genai.Client` reale offline, es. httpx mock transport),
così la rottura è in CI e non in prod. Migrare a un'eventuale API pubblica di raw-request.

---

### ISSUE-006 — I metodi nativi risolvono via slot capability `"chat.completions"`: la matrice del gateway non protegge

**Priorità:** medium
**Stato:** open
**File coinvolti:** `infrastructure/llm/gateway.py:117-160` (i quattro metodi nativi fanno `_resolve(model.provider, "chat.completions")`); unica guardia in `application/completion_service.py:225-229,256-260` (Anthropic) e `:314-318,364-368` (Gemini)

**Problema**
I quattro metodi nativi del gateway risolvono l'adapter via lo slot `"chat.completions"` e
poi chiamano direttamente il metodo nativo, anche se solo `AnthropicAdapter`/`VertexAdapter`
li implementano (`OpenAIAdapter`/`AzureOpenAIAdapter`/`BedrockAdapter` no). La matrice di
capability (`gateway.py:39-69`), che per ogni altra operazione alza `UnsupportedOperation`
(→501) sui provider non supportati, è bypassata. L'unica cosa che impedisce
`gateway.anative_messages(...)` su un modello Bedrock è il singolo check
`if model.provider is not Provider.ANTHROPIC: raise ProviderMismatch(...)` nel layer
applicativo (duplicato per Gemini).

**Perché è un problema**
Manca la difesa-in-profondità proprio nel layer (`gateway.py`) che esiste per fornirla. Se
il check applicativo viene rimosso/refattorizzato, o un nuovo chiamante raggiunge il gateway
direttamente, il fallimento è un `AttributeError` non gestito → 500 opaco, non il 501 pulito
che il resto del codice garantisce (contraddice la docstring del file).

**Impatto possibile**
Manutenibilità / trappola per bug latenti su future aggiunte di provider; potenziale 500
opaco al posto di 501.

**Soluzione consigliata**
Aggiungere `"native.messages"`/`"native.generate_content"` come chiavi reali nel `_registry`,
gattare `_resolve` su di esse, e rendere la matrice del gateway autoritativa (mantenendo o
rimuovendo il check applicativo, ma così la sua rimozione non può produrre un 500).

---

### ISSUE-007 — Gli endpoint nativi emettono l'envelope errori OpenAI, non quello del provider; contratto non testato né documentato

**Priorità:** medium
**Stato:** open
**File coinvolti:** `infrastructure/web/api_router/router.py:49` (`exception_handlers={DomainError: openai_error_handler}` sull'intero router, inclusi i nativi); `infrastructure/web/exception_handlers.py:160-174`

**Problema**
Le route native vivono sullo stesso `api_router` il cui handler `DomainError` emette
l'envelope OpenAI `{"error": {"message","type","code"}}`. Un errore di dominio su
`/v1/messages` o `:generateContent` (404/409/402/501…) restituisce quindi la forma OpenAI, non
quella nativa di Anthropic (`{"type":"error","error":{...}}`) o Gemini
(`{"error":{"code","message","status"}}`). Funziona oggi solo perché gli SDK derivano il
**tipo** di eccezione dallo status HTTP, non dal body. Nessun test asserisce la forma del
body su una route nativa (grep: zero asserzioni body-shape sui test native/conformance).

**Perché è un problema**
La premessa degli endpoint nativi ("punta l'SDK stock e funziona") si estende alla gestione
errori che un client sofisticato può ispezionare (`exc.body["error"]["type"]`, parsing del
retry-reason). Nulla fissa se il body debba restare OpenAI-shaped (scelta difendibile e più
semplice) o diventare nativo, e non esiste regressione in nessuna delle due direzioni.

**Impatto possibile**
Ergonomia degli errori per i client nativi (non correttezza né sicurezza: lo status è giusto,
quindi retry/backoff su 429/5xx funzionano). Debito di test/contratto.

**Soluzione consigliata**
Decidere e **documentare** il contratto di body errori delle superfici native (OpenAI-shaped
è difendibile — dirlo in `docs/native-*.md`) e aggiungere almeno un test per superficie che
asserisce la forma del body su un errore di dominio, non solo lo status.

---

### ISSUE-008 — La stima prompt (`_request_text`) ignora il campo top-level `system` di Anthropic

**Priorità:** low
**Stato:** open
**File coinvolti:** `application/usage_meter.py:38-62` (`_request_text`), consumato da `_reservation_cost` (`:149-160`) su `completion_service.py:230,261`

**Problema**
Il body nativo Anthropic porta il system prompt in un campo top-level `system` (stringa o
lista di blocchi), separato da `messages`; il contenuto dei messaggi può includere blocchi
`tool_use`/`tool_result`/immagini. `_request_text` legge solo `messages[].content` testuale,
mai `system`.

**Perché è un problema**
Under-count del lato prompt della reservation pessimistica per le chiamate Anthropic native
(es. un grande system prompt è invisibile alla reservation). Impatto limitato: il termine
dominante (`max_tokens`, il ceiling di output) **è** catturato per Anthropic, e il bill finale
è sempre settlato sull'usage autorevole del provider — quindi non indipendentemente
sfruttabile a scala significativa.

**Impatto possibile**
Reservation leggermente sotto-stimata (accuratezza del throttle in-flight), non del bill.

**Soluzione consigliata**
Aggiungere `system` (stringa o lista di blocchi) e altri campi prompt fuori da `messages`
all'estrazione di `_request_text` quando presenti.

---

### ISSUE-009 — Triplicazione dello scheletro di stream-metering (debito DRY)

**Priorità:** low
**Stato:** open
**File coinvolti:** `application/completion_service.py:270-295` (`_metered_native`), `:378-404` (`_metered_gemini`), `:515-543` (`_metered`); `application/usage_meter.py:392-455` (`metered_stream`), `:456-515` (`metered_native_stream`), `:516-569` (`metered_gemini_stream`)

**Problema**
Le parti critiche (release-esattamente-una-volta, settlement shielded, timeout, branching
error-vs-ok) sono correttamente già fattorizzate in `_finalize_stream_billing` (non
duplicate). Ma il loop di relay e la sua gestione eccezioni sono copiati tre volte,
differendo solo nelle 4-8 righe che estraggono usage/testo dalle forme di chunk OpenAI,
Anthropic e Gemini.

**Perché è un problema**
Un futuro fix alla struttura del loop (nuovo edge di cancellazione, o cambio di cosa conta
come "disconnect vs error") andrebbe portato a mano su tre call-site, e nulla ne garantisce
la sincronia. Non è ancora un bug (i tre sono mirror fedeli e ben testati), ma è il rischio
di divergenza da tenere d'occhio al prossimo cambio di stream-metering.

**Impatto possibile**
Manutenibilità; rischio di divergenza futura.

**Soluzione consigliata** (non urgente)
Estrarre lo scheletro condiviso in un `_metered_wire_stream(..., extract_usage, extract_text)`
generico e far diventare i tre metodi pubblici sottili binding di parametri. Dato "simplicity
first" e che la triplicazione è piccola e consistente, è un nice-to-have, non un blocker.

---

### ISSUE-010 — Asimmetria di copertura test: nessun test di budget/concorrenza sulla superficie Gemini nativa

**Priorità:** low
**Stato:** open
**File coinvolti:** `tests/native/test_generate_content.py` (nessun riferimento a `budget`/`402`/`BudgetExceeded`); contrasto con `tests/native/test_messages.py` (`test_over_budget_rejected_at_admit_and_not_dispatched`)

**Problema**
Esiste un test di over-budget per Anthropic nativo ma non l'equivalente per Gemini. Combinato
con ISSUE-002 (reservation Gemini $0), è proprio l'area dove un test avrebbe colto il bug.

**Perché è un problema**
Il path `admit()` è condiviso e testato via Anthropic + superficie OpenAI, ma l'assenza di un
test Gemini-nativo di budget/concorrenza è esattamente ciò che ha lasciato passare ISSUE-002.

**Impatto possibile**
Copertura; rischio di regressione non individuata sulla superficie Gemini.

**Soluzione consigliata**
Aggiungere a `test_generate_content.py` il test di over-budget (e, per ISSUE-002, un test di
burst concorrente che asserisce reservation non-zero e `BudgetExceeded`).

## Recommended resolution order

1. **ISSUE-001** (critical) — chiudere il vettore di override credenziale / open-relay su
   Anthropic nativo. Bloccante per qualsiasi traffico di produzione sul surface nativo.
2. **ISSUE-003** (high) — applicare il ceiling `max_output_tokens` + clamp sul path nativo
   (spesa upstream reale non limitata; allinea entrambe le superfici).
3. **ISSUE-002** (high) — reservation Gemini-aware (ripristina la guard in-flight burst).
4. **ISSUE-004** (high) — far passare `generate_content` per `_dispatch`/passare `request`
   (elimina il billing $0 silenzioso).
5. **ISSUE-005** (medium) — cap del pin `google-genai` + wrapper/test sull'API privata.
6. **ISSUE-006** (medium) — chiavi capability native nella matrice del gateway.
7. **ISSUE-007** (medium) — decidere/documentare + testare il contratto di body errori nativo.
8. **ISSUE-008** (low) — includere `system` in `_request_text`.
9. **ISSUE-009** (low) — (opzionale) fattorizzare lo scheletro di stream-metering.
10. **ISSUE-010** (low) — test di budget/concorrenza Gemini nativo (chiude anche il gap di ISSUE-002).

Nota: ISSUE-001/002/003/004 sono tutte istanze dello stesso tema radice — il "verbatim
passthrough" salta la sanitizzazione/clamp/reservation che la superficie OpenAI applica.
Un intervento coeso su `prepare_native` (validazione + clamp + vista-reservation per-provider)
chiude ISSUE-001, ISSUE-002 e ISSUE-003 insieme.

## Final assessment

Progetto complessivamente solido e maturo: sette round di hardening hanno reso il core
(sicurezza, tenancy, money, streaming, migrazioni, CI) robusto, e la parte *difficile* del
nuovo surface nativo — settlement exactly-once, disconnect/cancellation, priming H24,
lifecycle client, accuratezza del billing settlato — è implementata bene e ben testata.

Il debito di questo round è concentrato e monotematico: la superficie **provider-native**,
introdotta rapidamente, ha preso "passthrough verbatim" **troppo alla lettera sul lato input
e governance**. Il body del client è fidato più del dovuto (ISSUE-001, kwargs-injection →
override credenziale, CRITICAL) e la governance che la superficie OpenAI applica (clamp del
ceiling di output, reservation di budget, stima H14) non è specchiata sul path nativo
(ISSUE-002/003/004). Nessuno richiede un redesign: sono fix localizzati che riportano il
surface nativo in linea con le guardie già esistenti, e sono naturalmente accorpabili in un
unico intervento su `prepare_native` + gli adapter.

Aree di miglioramento principali: (1) trattare il body nativo come **input non fidato**
(denylist dei kwargs di controllo SDK, clamp del ceiling, reservation per-provider); (2)
ridurre la dipendenza da API private di SDK con pin e test di contratto; (3) rendere la
matrice di capability del gateway autoritativa anche per le operazioni native; (4) colmare i
gap di test (budget/concorrenza Gemini, forma del body errori nativo). Con la CRITICAL e le
tre HIGH chiuse, il surface nativo è pronto per il traffico di produzione.

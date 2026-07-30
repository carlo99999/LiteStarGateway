# Code Review — Round 15 (delta dopo `5469464`)

[← Index](INDEX.md)

Review dell'intero delta successivo al commit `5469464` ("chore: update
knowledge graph after the Round 14 remediation"), fino a `HEAD`
(`d7e98af`, branch `feat/guardrail-router-scope`). Il range copre 185 file e
~16k righe: il provider generico **openai_compatible** con egress allowlist
fail-closed (Plan 18), i **guardrail** (port, chain runner, provider
webhook/judge, policy store/API, scope per-router — Plan 06 + `d7e98af`), le
**prenotazioni di budget distribuite** su Redis e il **denaro Decimal/NUMERIC**
(Plan 17 Slice 3/5), i **budget per-chiave** (Slice 7), il **segreto webhook
per-team** e il **replay degli alert**, le **capability dichiarate per modello**
e la **console di affidabilità**.

Sei reviewer indipendenti hanno coperto in parallelo sei lenti (egress/webhook
×2, correttezza guardrail ×2, billing/concorrenza/persistenza ×2, API/UI); il
coordinatore ha ri-tracciato ogni finding consequenziale end-to-end sul tree
corrente e assegnato la severità. Sono inclusi solo difetti riproducibili o
deterministici. Modalità sola-review: **nessun file di prodotto è stato
modificato**.

Baseline eseguita sul tree corrente:

- `uv run pytest -q --cov=src/litestar_gateway --cov-fail-under=80`:
  **2184 passed, 17 skipped**, copertura **93,13%** (223,96 s);
- `uv run ruff check` → *All checks passed*; `uv run ruff format --check` →
  495 file già formattati; `uv run pyrefly check` → **0 errori** (345
  soppressi);
- `uv run pip-audit` → nessuna vulnerabilità nota (solo il package locale non
  su PyPI è skippato);
- `just docs-build` (`mkdocs build --strict`) → verde;
- `just ui-schema-check` → schema/typed-client allineati; `just ui-ci`
  (lint + build) → verde;
- `git diff --check 5469464..HEAD` → **marker di conflitto lasciati committati**
  (ISSUE-050).
- **Non eseguiti**: `just test-postgres` e `just test-redis` (i 9 test di
  conformità Redis skippano senza `REDIS_TEST_URL`; l'adapter in-memory della
  stessa suite passa). Le conclusioni su Redis sono quindi *strongly supported
  inference* dal codice/Lua, non riprodotte contro un Redis reale.

## Executive summary

La base storica regge: l'estrazione delle primitive di egress (deny-list) è
byte-for-byte, il matcher allowlist è fail-closed su ogni bordo, lo schema di
firma HMAC dei webhook è corretto (timestamp legato, `compare_digest`,
tolleranza ±300 s), il core Decimal/NUMERIC ha una sola regola di
arrotondamento e una migrazione con cast esplicito, l'atomicità delle
prenotazioni Redis vive in un unico script Lua con release idempotente, e la
tenancy/RBAC delle nuove CRUD guardrail è doppio-filtrata per `team_id`.

Il tema dei difetti nuovi è invece uno solo, ripetuto su tre feature diverse:
**un controllo dichiarato viene aggirato su un percorso alternativo che non lo
richiama.** I guardrail — la feature che apre più mercato in questo delta —
sono corretti sul percorso non-stream OpenAI, ma la redazione viene annullata
sul retry di failover (ISSUE-035), il body pre-guardrail viene servito da cache
(ISSUE-036), gli endpoint native saltano del tutto il guard di richiesta
(ISSUE-038), lo streaming salta il guard di risposta (ISSUE-042) e due
redattori in catena non compongono (ISSUE-039). In parallelo, il budget
**per-chiave** appena introdotto non viene applicato sulla superficie OpenAI —
cioè su quasi tutto il traffico (ISSUE-037) — e l'allowlist di egress non è
ricontrollata al dispatch nonostante docstring e piano lo dichiarino
(ISSUE-034). Nessuno di questi è un'escalation cross-tenant, ma ciascuno
vanifica silenziosamente una garanzia che l'operatore crede attiva.

Counts: **0 CRITICAL · 7 HIGH · 6 MEDIUM · 6 LOW**.

## Issue summary

| ID | Title | Severity | Files | Status |
|---|---|---|---|---|
| ISSUE-034 | L'allowlist egress `openai_compatible` non è applicata al dispatch (rebinding DNS + revoca no-op) | HIGH | `application/egress.py`; `application/credential_service.py`; `infrastructure/llm/openai_adapter.py` | Fixed by #443 |
| ISSUE-035 | Il retry di failover invia il prompt NON redatto al provider di fallback | HIGH | `application/completion_service.py` | Fixed by #446 |
| ISSUE-036 | La response cache memorizza e riserve il body pre-guardrail (bypass BLOCK/REDACT) | HIGH | `application/completion_service.py` | Fixed by #446 |
| ISSUE-037 | Il budget per-chiave non è applicato sulla superficie OpenAI (`/v1/chat`, `/v1/responses`, embeddings, images) | HIGH | `application/completion_service.py`; `application/usage_meter.py` | Fixed by #449 |
| ISSUE-038 | Gli endpoint native bypassano il guard di richiesta e il guard di risposta è un no-op | HIGH | `application/completion_service.py`; `application/guardrails/payloads.py` | Fixed by #447 |
| ISSUE-039 | REDACT non compone: l'ultimo redattore vince, le redazioni precedenti si perdono | HIGH | `application/guardrails/service.py` | Fixed by #445 |
| ISSUE-040 | Il purge del team omette le regole guardrail team-wide → purge negato/definitivamente bloccato | HIGH | `infrastructure/persistence/team_repository.py`; `persistence/orm.py`; migration guardrail_rule | Fixed by #444 |
| ISSUE-041 | Lo scope di una regola guardrail non è allargabile/commutabile via PATCH; la console lo offre e risponde 200 | MEDIUM | `application/guardrail_policy_service.py`; `web/guardrails/schemas.py`; `ui/.../guardrails` | Fixed by #453 |
| ISSUE-042 | Lo streaming salta il guardrail di risposta bloccante (default sicuro non implementato) | MEDIUM | `application/completion_service.py` | Fixed by #448 |
| ISSUE-043 | `ALLOWED_HOSTS=*` supera il gate di startup e disabilita la validazione dell'Host | MEDIUM | `config.py`; `app.py` | Fixed by #450 |
| ISSUE-044 | La prenotazione key-scope resta appesa quando il gate team rifiuta dopo quello key | MEDIUM | `application/usage_meter.py` | Fixed by #449 |
| ISSUE-045 | Release delle prenotazioni non fail-safe / cache-hit stream non shieldato | MEDIUM | `application/usage_meter.py`; `application/completion_service.py` | Fixed by #449 |
| ISSUE-046 | Il guardrail judge non ha time budget (eredita il timeout gateway da 60 s) | MEDIUM | `application/guardrails/judge.py`; `domain/guardrail_config.py` | Fixed by #451 |
| ISSUE-047 | Segreto webhook per-team stringa vuota → fallback silenzioso al segreto di piattaforma | LOW | `web/teams/schemas.py`; `persistence/budget_repository.py`; `notifications/channel_resolver.py` | Fixed by #453 |
| ISSUE-048 | `api_base` accetta userinfo (`user:pass@`) che finisce in `ClientKey.endpoint` loggato in chiaro | LOW | `application/credential_service.py`; `infrastructure/llm/openai_adapter.py` | Fixed by #443 |
| ISSUE-049 | Unicità del nome regola guardrail check-then-write → 500 sotto concorrenza | LOW | `application/guardrail_policy_service.py` | Fixed by #453 |
| ISSUE-050 | Marker di conflitto di merge committati in doc pubblicate nella nav mkdocs | LOW | `plans/README.md`; `plans/18-openai-compatible-provider.md`; `docs/next-steps/openai-compatible-provider.md` | Fixed by #442 |
| ISSUE-051 | Il client sync `openai_compatible` ha `follow_redirects=True` (latente) | LOW | `infrastructure/llm/openai_adapter.py`; `infrastructure/llm/resilience.py` | Fixed by #442 |
| ISSUE-052 | La rotazione della chiave perde silenziosamente il cap per-chiave | LOW | `infrastructure/web/teams/controller.py` | Fixed by #452 |

## Findings

### ISSUE-034 — L'allowlist egress `openai_compatible` non è applicata al dispatch (HIGH)

**Dove.** `src/litestar_gateway/application/credential_service.py:51-69` (`_validate_egress`,
chiamato in create `:76` e update `:101`) è l'**unico** chiamante di
`resolve_allowlisted_addresses` (`src/litestar_gateway/application/egress.py:73-96`).
Il dispatch `LLMGatewayImpl` → `OpenAICompatibleProviderAdapter._async_client`
(`src/litestar_gateway/infrastructure/llm/openai_adapter.py:246-280`) costruisce
`AsyncOpenAI(base_url=credentials["api_base"], ...)` senza alcun controllo egress.

**Problema.** La docstring di `egress.py:76-80` e quella di
`credential_service.py:53-55` affermano che l'allowlist è *"re-resolved on every
call ... refused at call time and not merely at config-save time"*, e Plan 18
elenca la resistenza al rebinding come regressione richiesta. Nessuna
ri-risoluzione avviene al dispatch: un `git grep` conferma un solo chiamante di
`resolve_allowlisted_addresses` in `src/`, il credential service.

**Perché è un problema.** Due invarianti dichiarate non reggono:

1. **Rebinding DNS.** Un allowlist per *hostname* (il caso naturale, es.
   `vllm.internal:8000`) è verificato contro una risposta DNS presa una sola
   volta, al salvataggio. Al call time l'SDK ri-risolve il nome e si connette
   dove punta ora — `169.254.169.254`, l'API server del cluster, qualsiasi host
   interno — spedendo i prompt del tenant **e la `api_key`** della credenziale.
   A differenza del path webhook (`post_to_approved_address`), l'IP non è
   nemmeno pinnato.
2. **Revoca no-op.** Svuotare o restringere `OPENAI_COMPATIBLE_ALLOWED_HOSTS`
   non ferma le credenziali esistenti: ogni completion continua a raggiungere
   l'`api_base` memorizzato. La proprietà "empty allowlist ⇒ provider
   inusabile" vale solo per create/update.

**Impatto verificato.** Confermato per lettura end-to-end: nessun controllo tra
`_resolve` e la costruzione del client; l'unico test di rebinding
(`tests/egress/test_egress_allowlist.py:138`) esercita la primitiva isolata,
quindi passa mentre la proprietà non vale per nessuna richiesta reale.
Classificazione: **Confirmed**. Con allowlist per-hostname e DNS controllabile
da un avversario il caso peggiore (exfil di credenziale + SSRF verso metadata)
si avvicina a CRITICAL; la severità resta HIGH per il prerequisito (controllo
del DNS del target configurato).

**Correzione suggerita.** Passare l'`EgressAllowlist` all'adapter e chiamare
`resolve_allowlisted_addresses(host, port, allowlist)` prima di costruire il
client, ripiegando il risultato in `ClientKey` (così un rebind invalida il
client in cache) e pinnando l'IP con Host/SNI come fa il path webhook. Test:
un target che risolve a un indirizzo allowlisted alla write e a uno non
allowlisted alla call ⇒ la *call* fallisce; una credenziale creata con
allowlist popolata smette di funzionare dopo lo svuotamento.

### ISSUE-035 — Il retry di failover invia il prompt NON redatto al provider di fallback (HIGH)

**Dove.** `src/litestar_gateway/application/completion_service.py:1341-1344`
(non-stream) e il gemello stream in `open_chat_stream` (`~:1594-1601`).

**Problema.** `_prepare` esegue `_guard_request` e restituisce il body redatto
`clean` (`:1115`), documentando che *"everything after this line ... sees the
redacted prompt and never the original"* (`:1110-1114`). Il tentativo #1 usa
`clean`; i tentativi #2+ ricostruiscono però da `sanitized` — il body
**pre-guardrail** (`:1132`, passato in `_chat_completion_with_failover` a
`:1150`):

```python
attempt_clean = clamp_output_tokens("chat.completions", sanitized, attempt_model.max_output_tokens)
attempt_clean = validate_chat_request(attempt_model, attempt_clean)
```

La chain di richiesta non viene ri-eseguita per il nuovo candidato.

**Perché è un problema.** Con una regola REDACT (es. redattore PII via webhook)
su un router failover-enabled: il tentativo #1 invia il prompt redatto, il
provider A fallisce (failover-eligible), il tentativo #2 invia a un provider B
diverso il prompt **con la PII ripristinata** — esattamente il "leak di prompt
non screenato su un exception path" che Plan 06 vieta. In più, la `_dispatch`
di failover (`:1356-1367`) omette `router_id`, quindi anche le regole di
*risposta* scoped-per-router non scattano sul path failover (vedi anche il
motivo dietro `d7e98af`).

**Impatto verificato.** Traccia deterministica sul codice: `clean` è l'output
di `_guard_request`, `sanitized` è il body pre-guardrail, e i tentativi 2+
ripartono da `sanitized`. Nessun test in `tests/guardrails/` tocca il path
failover. Classificazione: **Confirmed**.

**Correzione suggerita.** Derivare i tentativi da `clean` (la redazione è
provider-independent; solo clamp/validate sono per-modello), oppure ri-invocare
`_guard_request` per ogni tentativo, e passare `router_id` alla `_dispatch` di
failover. Regressione: un REDACT + failover con candidato #1 in errore ⇒ il body
che arriva al candidato #2 è redatto.

### ISSUE-036 — La response cache memorizza e riserve il body pre-guardrail (HIGH)

**Dove.** `src/litestar_gateway/application/completion_service.py:426`
(`_cache_put`) e `:428` (`_semantic_put`) eseguono **prima** di
`_guard_response` (`:444`); i path di hit ritornano il body senza chain di
risposta: `:389` (non-stream) e `_metered_cache_hit_stream` (`:1525-1531`).

**Problema.** Alla *miss*, il body grezzo del provider è scritto in cache a
`:426`, poi `_guard_response` blocca/redige a `:444`. Alla *hit* (`:376-389`)
si ritorna `cached.body` senza guard.

**Perché è un problema.** Con response cache attiva su un modello e una regola
RESPONSE di BLOCK/REDACT: la richiesta #1 viene rifiutata/redatta al chiamante,
ma il body non screenato è già in cache; la richiesta #2 identica riceve una
*hit* e ottiene il contenuto bloccato/non redatto verbatim, senza mai passare
la chain. Il contenuto bloccato viene inoltre **persistito** nello store di
cache. Vale anche per il tier semantico e per il replay stream sintetico.

**Impatto verificato.** Confermato per lettura: ordine `_cache_put`(426) →
`_guard_response`(444), e ritorno a `:389`/`:1528` senza `_guard_response`.
Classificazione: **Confirmed**. Distinto da R13 #393/#394 (che riguardava la
*chiave* di cache non discriminante per modello/operazione/policy): qui la
chiave è corretta, ma il *contenuto* memorizzato è pre-guardrail.

**Correzione suggerita.** Spostare `_cache_put`/`_semantic_put` **dopo**
`_guard_response` (cache del body screenato) ed eseguire `_guard_response` anche
sulle hit; oppure non cache-are quando esiste una chain RESPONSE non vuota.

### ISSUE-037 — Il budget per-chiave non è applicato sulla superficie OpenAI (HIGH)

**Dove.** `src/litestar_gateway/application/completion_service.py:1118`
(`_prepare`, punto di ammissione per `/v1/chat/completions`, `/v1/responses`,
embeddings, images) chiama `admit(team_id, model, ...)` **senza** `api_key_id`;
idem i retry di failover `:1348` e `:1601`. Solo i quattro call site *native*
(`:808`, `:843`, `:928`, `:963` — Anthropic `messages` e Gemini
`generate_content`, stream e non-stream) passano `api_key_id=api_key_id`.
In `UsageMeter._admit_key` (`src/litestar_gateway/application/usage_meter.py:432-437`)
il gate fa short-circuit: `if self._api_key_budgets is None or api_key_id is None: return None`.

**Problema.** Il wiring è completo
(`api_key_budgets=SQLAlchemyApiKeyBudgetRepository(...)` in
`infrastructure/web/api_router/dependencies.py:139`), quindi il gap è il solo
argomento omesso: il cap per-chiave non è mai valutato sul traffico OpenAI.

**Perché è un problema.** Una chiave con cap block `$10/giorno` spende fino
all'intero budget del team attraverso `/v1/chat/completions`; il cap vincola
solo su `/v1/messages` e `generateContent`. La feature Plan 17 Slice 7 è di
fatto un no-op su ~tutto il traffico reale. `tests/budgets/test_per_key_budgets.py`
prova `meter.admit(..., api_key_id=KEY_ID)` direttamente e
`tests/teams/test_key_budget_api.py` prova solo CRUD/RBAC: nessun test end-to-end
sul path OpenAI.

**Impatto verificato.** Confermato per lettura: firma di `admit` con
`api_key_id` keyword-only default `None` (`usage_meter.py:391-399`), i 4 soli
call site che lo passano sono native, `_prepare:1118` no. Classificazione:
**Confirmed**.

**Correzione suggerita.** Propagare `api_key_id` in `_prepare.admit` e nei due
`admit` di failover (mantenendo `skip_team_rate_limit=True` per l'RPM). Test
end-to-end: cap block superato ⇒ `/v1/chat/completions` con quella chiave viene
rifiutato.

### ISSUE-038 — Gli endpoint native bypassano il guard di richiesta e il guard di risposta è un no-op (HIGH)

**Dove.** `src/litestar_gateway/application/completion_service.py:745-781`
(`prepare_native`) non chiama `_guard_request`; l'estrazione del testo di
risposta `guardrails/payloads.py:41-52` (`response_text`) legge solo
`choices[].message.content` e `output[].content`.

**Problema.** `/v1/messages` (Anthropic) e Gemini `generateContent` — stream e
non-stream — inviano i prompt senza screening di richiesta anche per un team i
cui path OpenAI sono guardati. Sul lato risposta la chain gira (via `_dispatch`)
ma `response_text` su un body native (blocchi `content` Anthropic, `candidates`
Gemini) restituisce `""`, cioè un ALLOW incondizionato per un judge/moderatore.

**Perché è un problema.** Un chiamante con chiave elude ogni guardrail di
richiesta configurato semplicemente usando la forma wire native dello stesso
modello. Le "Known limits" della doc non menzionano questo buco.

**Impatto verificato.** Confermato: `prepare_native` esegue solo
`reject_native_control_kwargs` + `clamp_native_output_tokens`; `response_text`
non conosce le forme native. Classificazione: **Confirmed** per il lato
richiesta; **strongly supported inference** per il no-op di risposta native
(dipende dal fatto che i body native non espongono `choices`/`output`).

**Correzione suggerita.** Cablare `_guard_request` in `prepare_native` con
estrattori di testo native, ed estendere `response_text` alle forme
Anthropic/Gemini; in alternativa rifiutare le chiamate native quando il team ha
una regola REQUEST attiva.

### ISSUE-039 — REDACT non compone: l'ultimo redattore vince (HIGH)

**Dove.** `src/litestar_gateway/application/guardrails/service.py:72-89`.

**Problema.** Tutti i provider girano concorrenti sullo **stesso** payload
originale (`asyncio.gather(*(_check(c, payload) ...))`), poi:

```python
text = payload.text
for verdict in results:
    if verdict.decision is Decision.REDACT and verdict.redacted_text is not None:
        text = verdict.redacted_text   # overwrite, non pipeline
```

**Perché è un problema.** Con due redattori (A maschera le email, B i numeri di
carta) il testo finale è la riscrittura di B **sull'originale**: la redazione di
A è persa e la PII va upstream. La docstring del modulo e `docs/guardrails.md`
affermano il contrario (*"the next one sees the rewritten version"*). Il test
`test_redactions_compose_in_chain_order` (`tests/guardrails/test_chain.py:134-144`)
usa redattori input-independent, quindi benedice la semantica rotta.

**Impatto verificato.** Confermato per lettura del loop. Con gli attuali tipi di
provider solo il webhook redige, ma incatenare due webhook redattori è
pienamente configurabile. Classificazione: **Confirmed** (impatto reale =
inference sulla configurazione a ≥2 redattori).

**Correzione suggerita.** Girare i provider concorrenti solo per il rilevamento
BLOCK, poi ri-eseguire i provider REDACT-capable in sequenza sull'accumulato;
oppure eseguire l'intera chain sequenzialmente nell'ordine configurato.

### ISSUE-040 — Il purge del team omette le regole guardrail team-wide (HIGH)

**Dove.** `src/litestar_gateway/infrastructure/persistence/team_repository.py:203-218`
(la lista dei figli cancellati **non** contiene `GuardrailRuleModel`);
`persistence/orm.py:797` — `team_id: Mapped[UUID] = mapped_column(ForeignKey("team.id"), index=True)`
senza `ondelete` (quindi RESTRICT); la migration
`2026-07-29_guardrail_rule_table_5363a209ffc8.py:96` conferma la FK senza
ondelete. Le regole *model-scoped* e *router-scoped* cascadano
(`model_id`/`router_id` con `ondelete=CASCADE`), ma quelle **team-wide**
(`model_id`/`router_id` NULL — la configurazione comune) no.

**Problema.** Il commento a `team_repository.py:169-170` afferma che la lista è
*"every table that carries this team's id"*, ma `guardrail_rule` porta `team_id`
ed è omessa. La `DELETE FROM team` (`:218`) solleva quindi `IntegrityError`,
catturata e ri-sollevata come `TeamNotEmpty` (409, `:220-224`).

**Perché è un problema.** `delete_team` (`application/team_service.py:280-306`)
su un team con storico di fatturazione fa `soft_delete` (tombstone); poi
`purge_team` (`:365-389`) chiama `team_repository.delete` che va sempre in 409.
Il purge, dichiarato *"irreversibile/completo"*, è così **permanentemente
bloccato** per qualsiasi team che abbia mai configurato una regola team-wide, e
i segreti webhook envelope-encrypted della regola **sopravvivono** al purge.
È la classe di ISSUE-030 (#401) reintrodotta per la nuova tabella, con un
impatto peggiore (blocco permanente + persistenza del segreto).
`tests/teams/test_retention_lifecycle.py` non ha copertura guardrail.

**Impatto verificato.** FK senza ondelete + assenza dalla lista figli
confermate. Il blocco del purge è deterministico. Classificazione:
**Confirmed** per il 409; **strongly supported inference** per la non-purgabilità
permanente (dipende dal fatto che una regola su un team tombstoned non sia più
cancellabile via API, dato che le CRUD guardrail risolvono il team via
permesso).

**Correzione suggerita.** Aggiungere `GuardrailRuleModel` alla lista di purge
(cancellandolo per `team_id` prima del `DELETE FROM team`), o dare alla FK
`ondelete=CASCADE`. Regressione: creare una regola team-wide, poi
delete→purge del team ⇒ purge completa.

### ISSUE-041 — Lo scope di una regola guardrail non è allargabile/commutabile via PATCH (MEDIUM)

**Dove.** `src/litestar_gateway/application/guardrail_policy_service.py:119`
(`applied = {k: v for k, v in changes.items() if v is not None}`);
`web/guardrails/schemas.py:96-101` (`model_id`/`router_id` default `None` senza
sentinella UNSET); UI `ui/src/features/guardrails/ruleForm.ts` +
`GuardrailsPage.tsx` che sottomettono lo scope "all models (team-wide)" via PATCH.

**Problema.** `model_id=None`/`router_id=None` sono indistinguibili da "omesso",
quindi una regola model/router-scoped non può tornare team-wide, e commutare
model→router imposta entrambi i campi, che `guardrail_config.validate_rule`
(`:53-57`) poi rifiuta con 400.

**Perché è un problema.** La console offre esplicitamente "all models
(team-wide)" come opzione modificabile, la sottomette e riceve **200 OK** con la
form che si chiude in successo — mentre lo scope resta quello vecchio, più
stretto. L'operatore crede di aver allargato un controllo di sicurezza; copre
ancora un solo modello. Il cambio model→router dà invece un 400 confuso per uno
stato che l'utente non ha mai inserito. È misconfigurazione silenziosa di un
controllo di sicurezza con risposta di successo.

**Impatto verificato.** Confermato su backend (filtro `is not None`), schema
(nessuna sentinella) e UI (submit del PATCH completo). Classificazione:
**Confirmed**.

**Correzione suggerita.** Dare ai due campi scope una sentinella UNSET
(msgspec `UNSET` o sentinella di modulo) in `UpdateGuardrailRuleRequest`,
trattare il null esplicito come "azzera", applicare la coppia `model_id`/
`router_id` atomicamente. Regressione: scoped→team-wide e model→router.

### ISSUE-042 — Lo streaming salta il guardrail di risposta bloccante (MEDIUM)

**Dove.** L'unico call site di `_guard_response` è la `_dispatch` non-stream
(`src/litestar_gateway/application/completion_service.py:444`). Nessuno dei path
stream — `open_chat_stream` (`:1423`), `open_responses_stream` (`:1665`),
`open_native_messages_stream` (`:820`), `open_generate_content_stream` (`:941`) —
esegue una chain di risposta, e non esiste un gate "rifiuta lo streaming quando
è attivo un guardrail di risposta bloccante".

**Problema.** Il design di Plan 06/`docs/next-steps/guardrails.md` prescrive come
**default sicuro** *"Block-streaming-when-active"* (disabilita SSE per il modello
quando è configurato un guardrail di risposta bloccante). Quel default non è
implementato: lo streaming semplicemente non guarda la risposta.

**Perché è un problema.** Un chiamante aggira una regola RESPONSE di
BLOCK/REDACT impostando `stream=true`. La doc lo elenca tra le "Known limits"
("Streaming responses are not guarded on the response side"), ma l'operatore che
configura una regola bloccante dalla console non ha alcun segnale che sia nulla
per gli stream. Nessun chunk già emesso viene ritirato (corretto), ma nulla è
nemmeno applicato.

**Impatto verificato.** Confermato per assenza (`_guard_response` solo a `:444`).
Classificazione: **Confirmed**. È un difetto (default sicuro del design non
onorato), non solo una limitazione documentata.

**Correzione suggerita.** All'apertura dello stream, risolvere la chain RESPONSE
e rifiutare lo stream (422) quando non è vuota, finché non arriva lo scan
incrementale a finestra; oppure fare buffer server-side + moderazione +
emissione non-stream.

### ISSUE-043 — `ALLOWED_HOSTS=*` supera il gate di startup e disabilita la validazione dell'Host (MEDIUM)

**Dove.** `src/litestar_gateway/config.py:390-395` valida solo la non-vuotezza
(`if not self.allowed_hosts: raise ...`); `app.py:174-178` passa la lista a
`AllowedHostsConfig`; il middleware Litestar fa early-return su `*`
(`if any(host == "*" ...): return`, `allowed_hosts_regex` resta `None` → ogni
Host accettato).

**Problema.** Il gate reso obbligatorio in questo delta per chiudere la classe
ISSUE-028/032 è banalmente e silenziosamente aggirabile: `ALLOWED_HOSTS=*` — la
cosa ovvia che un operatore scrive per superare un nuovo setting obbligatorio —
passa la validazione e ripristina il comportamento pre-fix, senza alcun warning.

**Perché è un problema.** Ripristina esattamente il "qualsiasi Host è accettato,
e tutto ciò che ne deriva è controllabile dall'attaccante" che il gate doveva
eliminare. In più il middleware fa `fullmatch` sull'Host **inclusa la porta**:
`ALLOWED_HOSTS=gw.example.com` fa 400 su `Host: gw.example.com:8443` — footgun
di disponibilità che spinge l'operatore verso `*`.

**Impatto verificato.** Confermato: la validazione controlla solo la
non-vuotezza; il ramo `*` del middleware è nel sorgente vendorizzato.
Classificazione: **Confirmed**.

**Correzione suggerita.** Rifiutare `*` (e le voci solo-`*.`) nella validazione
di `config.py`; documentare/normalizzare la porta nelle voci o nell'Host.

### ISSUE-044 — La prenotazione key-scope resta appesa quando il gate team rifiuta (MEDIUM)

**Dove.** `src/litestar_gateway/application/usage_meter.py:427-430`.

**Problema.** `admit` esegue `key_claim = await self._admit_key(...)` poi
`team_claim = await self._admit_team(...)` senza try/except intermedio. Se
`_admit_team` solleva `BudgetExceeded` (`:520-525`) o il call Redis fallisce, la
prenotazione key già registrata non viene mai rilasciata (il `finally` dei
chiamanti vede solo un `Admission` mai restituito). Il commento `:423-426` e il
test `test_a_refused_key_leaves_no_team_reservation_behind` coprono solo la
direzione inversa.

**Perché è un problema.** Ogni rifiuto del cap team, su una chiave con cap block,
lascia il costo pessimistico appeso nel pool key per TTL=300 s. Un burst di
retry riempie il cap key di spend fantasma in-flight → la chiave viene falsamente
rifiutata ("+X USD reserved by in-flight requests") anche senza nulla in volo.
Nessun overshoot: pura false-denial/DoS sulla chiave.

**Impatto verificato.** Riprodotto dal reviewer (store in-memory, cap key 100,
team a limite): tre `admit` rifiutati lasciano `0.011/0.022/0.033` USD appesi in
`key:<uuid>` per 300 s. Classificazione: **Confirmed**.

**Correzione suggerita.** Avvolgere `_admit_team` in try/except che rilascia
`key_claim` prima di rilanciare. Regressione: estendere
`test_the_team_cap_still_binds_a_key_with_a_larger_cap` con
`assert reserved(key_scope) == 0` dopo il rifiuto.

### ISSUE-045 — Release delle prenotazioni non fail-safe / cache-hit stream non shieldato (MEDIUM)

**Dove.** `src/litestar_gateway/application/usage_meter.py:559-566` e
`:1014-1017` (release senza gestione eccezioni; loop su
`admission.reservations`); `application/completion_service.py:447-448`
(`_dispatch` finally) e `:1525-1542` (`_metered_cache_hit_stream` finally **non**
shieldato).

**Problema.** (a) Con lo store Redis, un errore su `hdel` in `release` durante il
`finally` di `_dispatch` sostituisce una risposta **già settlata** con un 500;
nel `finally` di `metered_stream`, il release precede `_finalize_stream_billing`,
quindi un errore di release perde la fatturazione dell'intero stream. (b)
`_metered_cache_hit_stream` non usa `CancelScope(shield=True)` come il gemello
`metered_stream` (che documenta perché serve): un disconnect a metà replay
cancella il `release()` (checkpoint reale con Redis), il flag `released=True` è
settato prima dell'await quindi anche il fallback `weakref.finalize` skippa, e
la prenotazione resta appesa fino al TTL; `settle_cache_hit` è saltato.

**Perché è un problema.** Un blip Redis a metà volo o un disconnect a metà replay
producono double-spend (retry di una call già addebitata), perdita di
settlement, o prenotazioni stranded — l'opposto del design fail-safe di
`_record_usage` una schermata più in là. Invisibile in CI perché i test usano lo
store in-memory (release senza punti di sospensione).

**Impatto verificato.** **Strongly supported inference**: il comportamento sotto
cancellazione al checkpoint `hdel` è quello contro cui il codice gemello si
shielda esplicitamente; non riprodotto contro un Redis reale (fuori baseline).

**Correzione suggerita.** Shieldare il `finally` di `_metered_cache_hit_stream`;
rendere `release` best-effort (log + continua sul resto delle reservation, non
propagare a un successo già settlato); ordinare il settlement dello stream prima
del release, o renderlo indipendente dall'esito del release.

### ISSUE-046 — Il guardrail judge non ha time budget (MEDIUM)

**Dove.** `src/litestar_gateway/application/guardrails/judge.py:89-90`
(`await self._complete(...)` senza timeout); `domain/guardrail_config.py`
(`_JUDGE_KEYS` = `judge_model`/`block_categories`/`char_budget`, nessun
`timeout_ms`, a differenza del webhook); `infrastructure/llm/resilience.py:21`
(`timeout: float = 60.0` + retry).

**Problema.** Il judge eredita il timeout del gateway (60 s) più i retry, mentre
il webhook è cappato a 10 s e la doc stessa afferma che 10 s è già oltre ciò che
un client interattivo tollera.

**Perché è un problema.** Un judge-model upstream bloccato trattiene la richiesta
del chiamante — e la sua prenotazione di budget (presa in `judge_call.py`) — per
un minuto o più; con `fail_policy=closed` il "timeout → BLOCK" non scatta mai
perché nulla va in timeout a questo livello.

**Impatto verificato.** Confermato per assenza del knob e del wrapping.
Classificazione: **Confirmed** (l'entità della latenza dipende dai timeout
dell'adapter — inference).

**Correzione suggerita.** `asyncio.timeout` attorno alla `complete` con un
`timeout_ms` validato (default ~2000 ms, come previsto da Plan 06 per la
moderazione), risolvendo il timeout secondo la fail policy.

### Findings LOW

- **ISSUE-047 — Segreto webhook per-team stringa vuota → fallback silenzioso.**
  `web/teams/schemas.py:48` accetta `alert_webhook_secret: str | None` senza
  check di vuotezza; `persistence/budget_repository.py:89-94` cifra `""` e setta
  `has_alert_webhook_secret=True`; `notifications/channel_resolver.py:53-57` fa
  poi `(secret decifrato) or settings.webhook_signing_secret`. Un team che PUT
  `""` (credendo di disabilitare/ruotare) firma con il segreto di piattaforma
  mentre l'API riporta `has_alert_webhook_secret: true`. In più, omissione e
  `None` significano entrambi "mantieni": non esiste modo di **azzerare** un
  segreto per-team se non cancellando il budget. **Confirmed.** Fix: rifiutare
  segreti vuoti/whitespace al controller + semantica esplicita di clear.

- **ISSUE-048 — `api_base` con userinfo finisce in `ClientKey.endpoint` loggato.**
  `application/credential_service.py:58-63` valida solo schema + hostname, non
  l'userinfo; `infrastructure/llm/openai_adapter.py:274-280` mette `base_url`
  (potenzialmente `https://user:pass@host`) in `ClientKey.endpoint`, che la
  docstring dichiara *"in the clear, purely for observability"* e che
  `client_registry` logga. È il primo provider il cui unico campo obbligatorio è
  un URL e la cui `api_key` è opzionale, quindi `user:pass@` è la cosa naturale
  da incollare. **Confirmed** (nessuna validazione + endpoint loggato).
  Fix: rifiutare/strippare l'userinfo in `_validate_egress`.

- **ISSUE-049 — Unicità del nome regola guardrail check-then-write.**
  `application/guardrail_policy_service.py:143-148` lista-e-scandisce in Python,
  poi `add` committa senza gestire `IntegrityError` sul vincolo
  `uq_guardrail_rule_team_id (team_id, name)`: due create concorrenti con lo
  stesso nome producono un 500 invece del 400 che il commento promette. I repo
  fratelli (`budget_repository`, `api_key_budget_repository`) catturano già
  `IntegrityError`. **Confirmed** (impatto basso). Fix: catturare e mappare a
  `InvalidGuardrailRule`.

- **ISSUE-050 — Marker di conflitto di merge committati in doc pubblicate.**
  `plans/README.md:53-59` (in nav mkdocs, riga 104),
  `plans/18-openai-compatible-provider.md` (18 marker) e
  `docs/next-steps/openai-compatible-provider.md:131-156` contengono
  `<<<<<<< HEAD` / `=======` / `>>>>>>> c6986eb...`, lasciati dal merge
  `c6986eb`. `git diff --check` li segnala; `mkdocs build --strict` **non** li
  intercetta, quindi finiscono nel sito renderizzato. **Confirmed.** Fix:
  risolvere i tre file (solo documentazione, nessun codice di prodotto).

- **ISSUE-051 — Client sync `openai_compatible` con `follow_redirects=True`.**
  `infrastructure/llm/openai_adapter.py:266-272`: `_sync_client` non passa un
  `http_client`, quindi l'SDK costruisce `_DefaultHttpxClient` con
  `follow_redirects=True`; il path async è sicuro solo incidentalmente (usa il
  nostro `httpx.AsyncClient`, default `False`). Un `307` da un endpoint
  allowlisted evade il bound verso un target non vincolato (httpx droppa
  `Authorization` cross-origin, quindi la key non trapela, ma il target di
  egress sì). **Confirmed**; latente (nessun web-caller sync raggiungibile).
  Fix: `follow_redirects=False` esplicito su entrambi i costruttori.

- **ISSUE-052 — La rotazione della chiave perde il cap per-chiave.**
  `infrastructure/web/teams/controller.py:501-528`: la rotazione emette una
  chiave sostitutiva con nuovo id; `api_key_budget` è keyed per `api_key_id` e
  nulla copia il cap al successore (`ondelete=CASCADE` sulla vecchia chiave).
  La nuova chiave gira **senza cap**, senza segnale, e `key_spend_since`
  riparte da zero. **Confirmed**; territorio di product-decision. Fix: copiare
  il cap alla chiave ruotata o avvisare esplicitamente.

## Resolution status

**Tutti i 19 finding sono chiusi** (#442–#453), una PR per slice di
[Plan 19](../plans/19-round-15-remediation.md), ognuna con almeno una regressione
che falliva prima della fix. La review in sé non ha modificato codice di
prodotto; la remediation è arrivata subito dopo.

| PR | Slice | Finding |
|---|---|---|
| #442 | S1 | 050, 051 |
| #443 | S2 | **034**, 048 |
| #444 | S4 | **040** |
| #445 | S8 | **039** |
| #446 | S5 | **035**, **036** |
| #447 | S6a | **038** |
| #448 | S6b | 042 |
| #449 | S7 | **037**, 044, 045 |
| #450 | S3 | 043 |
| #451 | S10 | 046 |
| #452 | S12 | 052 |
| #453 | S9+S11 | 041, 047, 049 |

Sei correzioni hanno richiesto una decisione più larga del finding, e sono
registrate nelle rispettive PR:

- **034**: il controllo per-call è al dispatch, ma il **pinning dell'IP non è
  replicabile** attraverso l'SDK OpenAI (che costruisce le richieste da
  `base_url`, mentre il path webhook pinna per-request). Resta una finestra
  TOCTOU fra la nostra risoluzione e la connect dell'SDK: dichiarata, non chiusa.
- **035**: la fix ovvia (usare `clean` al posto di `sanitized`) era sbagliata —
  avrebbe fatto ereditare al secondo candidato il clamp del primo. La chain viene
  rieseguita per tentativo, il che chiude anche la metà del finding che il report
  aveva solo annotato (la chain non era risolta per il nuovo candidato).
- **039**: la catena è ora **sequenziale**, quindi paga la somma dei tempi e non
  il massimo. Un test verde che asseriva la concorrenza è stato **rimosso**: era
  la proprietà che causava il leak.
- **042**: il design chiedeva di rifiutare lo stream solo con una regola
  *bloccante*, condizione **non decidibile a priori** (dipende dal contenuto
  della risposta). Rifiutiamo con qualsiasi regola RESPONSE: un team con una
  regola di sola redazione perde lo streaming su quel modello.
- **037**: passare `api_key_id` ad `admit` avrebbe **dimezzato il rate limit di
  ogni chiave** (doppio hit RPM). Risolto con `skip_key_rate_limit`, simmetrico
  al flag team già esistente, e una regressione che fissa "un hit per richiesta".
- **040**: la migrazione dati prevista **non serviva** (il fix del codice sblocca
  i team già tombstoned, e la FK RESTRICT esclude righe orfane). Il difetto era
  inoltre più ampio del riportato: colpiva anche la `delete_team` ordinaria.

Due punti dove la copertura è dichiarata incompleta: lo **shield** del replay in
cache (045) non ha un test che lo riproduca — lo store in-memory rilascia senza
punti di sospensione — ed è parità difensiva col gemello coperto; e la metà
**porta** di 043 è documentata, non normalizzata, perché il match vive nel
middleware di Litestar.

ISSUE-034…052 erano difetti nuovi in codice nuovo del delta; dove pertinente
citano la classe storica correlata già chiusa (ISSUE-030/#401 per il purge,
ISSUE-028/032 per l'Host, R13 #393/#394 per la cache). Le remediation dei
Round 13/14 (#392–#405) sono state ricampionate a campione e reggono (vedi
*Verified clean*).

**La lezione trasversale.** In cinque casi su diciannove il difetto non era una
distrazione ma una **promessa non mantenuta già scritta**: le docstring di
`egress.py` e `docs/self-hosted-models.md` garantivano la ri-risoluzione per
chiamata (034), `docs/guardrails.md` garantiva che i redattori componessero
(039), `prepare_native` si dichiarava il punto centrale delle governance guard
(038), il commento del purge affermava di elencare "ogni tabella con questo
team_id" (040). In un sesto caso il design doc chiedeva due cose incompatibili —
`asyncio.gather` *e* composizione left-to-right — e l'implementazione l'ha seguito
fedelmente (039). Per il prossimo round: **leggere le promesse del codice come
asserzioni da verificare, non come contesto**. È stata la traccia più produttiva
di tutta la review. Il secondo pattern ricorrente, comparso tre volte in feature
diverse, è **due intenzioni compresse in un unico valore nullable** (041, 047, e
il `kind` ignorato in PATCH).

## Deferred / product decision

- **Firma webhook opt-in-by-warning** per i due sender preesistenti (routing
  `application/routing/webhook.py:181-197` e budget-alert
  `notifications/webhook_channel.py:116-133`): senza segreto configurato loggano
  un warning e inviano **non firmato**, mentre il provider webhook guardrail lo
  richiede hard. Scelta di retro-compatibilità documentata nelle docstring; ogni
  deployment che aggiorna senza settare il segreto invia prompt/spend non
  autenticati. Valutare un rifiuto allo startup quando un target webhook è
  configurato ma manca il segreto.
- **Post-guardrail dopo `settle_ok`** (non prima come diceva Plan 06):
  deliberato e documentato (`completion_service.py:439-443`) — un body bloccato
  va comunque fatturato o il guard diventa un canale gratuito. Concordo; il testo
  del piano andrebbe aggiornato.
- **Solo l'ultimo messaggio utente viene giudicato** (`payloads.py:102-109`):
  un jailbreak in un `system`/`assistant`/`tool` turn, o un transcript
  pre-costruito, passa il guard di richiesta. Non documentato: andrebbe almeno
  dichiarato nelle "Known limits".
- **Regole non applicabili a modelli/router globali o granted**
  (`guardrail_policy_service._ensure_model_visible`/`_ensure_router_visible`):
  la direzione di rifiuto è fail-safe, ma "guarda l'alias che il chiamante
  invoca" è inesprimibile per gli alias condivisi — follow-up.
- **Regola model-scoped che sovrascrive la team-wide per il traffico router**
  quando il router non ha regole proprie (`domain/entities/guardrail.py:102-105`):
  `by_model or team_wide` fa vincere il model-scoped, riaprendo un grado sotto lo
  stesso buco che lo scope router doveva chiudere. Da chiarire se intenzionale.
- **Reservation TTL 300 s vs stream lunghi**, **ammissione che esclude
  l'importo entrante**, **skew di clock wall vs monotonic tra store**: trade-off
  documentati e bounded, non alterati qui.
- **Ledger su float (R3-L15)**: scelta storica deliberatamente non toccata dalla
  decimalizzazione (le sole *rate* restano float, convertite al seam `rate_card`).

## Verified clean

- **Egress deny-list** estratta byte-for-byte da `routing/webhook.py`
  (`_literal_ip`, `_is_blocked` con gli stessi sei predicati, short-circuit
  letterale, blocco per-indirizzo); i tre sender (routing, budget-alert,
  guardrail) ri-risolvono per call e pinnano l'IP con Host/SNI via
  `post_to_approved_address`, `follow_redirects=False`.
- **Matcher allowlist** (`domain/egress_policy.py`): allowlist vuota rifiuta
  prima di risolvere; case-normalizzata; trailing-dot fail-closed; voce-con-porta
  vs URL-senza-porta nega; IPv6 bracketed + porta corretti; CIDR malformato
  solleva invece di degradare a hostname; le voci-indirizzo richiedono che
  **ogni** indirizzo risolto sia dentro la rete (niente split-DNS smuggling);
  parsing errato fallisce al `Settings.__post_init__` anche in local.
- **Firma HMAC webhook** (`domain/webhook_signature.py`): MAC su `{ts}.{body}`
  esatto trasmesso, timestamp legato, tolleranza ±300 s, `event_id` stabile per
  dedup, `compare_digest`. (Nota LOW separata sotto.)
- **Capability dichiarate**: validate su create e update, solo per
  `openai_compatible`, valori sconosciuti → 400; il gateway **interseca** con il
  massimo dell'adapter (`gateway.py:115-127`) e può solo restringere; op non
  supportata → `UnsupportedOperation` → 501; round-trip ORM fail-closed a
  chat-only.
- **Isolamento client pool**: `ClientKey(provider="openai_compatible")` disgiunto
  da `"openai"`; `PLACEHOLDER_API_KEY` costante non-segreta; `fingerprint_material`
  è SHA-256 troncato, nessun materiale segreto recuperabile.
- **Decimal/NUMERIC**: singola regola `ROUND_HALF_UP` a 6 dp applicata una volta
  in `compute_cost`; `Decimal(str(x))` a un solo seam; `Money` TypeDecorator
  quantizza in bind e result (SQLite ↔ PG coerenti); migration con cast
  `postgresql_using` e `batch_alter_table`; `MONEY_COLUMNS` copre tutte le
  colonne Money.
- **Atomicità prenotazioni** (Redis): sweep+sum+decide+record in un solo Lua;
  `release` HDEL per UUID server-generato, idempotente; `EXPIRE ttl*2` sopravvive
  a ogni field; suite di conformità parametrizzata su in-memory + Redis reale
  (two-replica, double-release, TTL, scope isolation).
- **Replay/requeue alert**: `requeue` è un UPDATE condizionale su `attempts >=
  MAX`, i righi in quarantena non sono mai selezionati dal drain, il claim CAS
  con lease è preservato (posture ISSUE-026/031), endpoint platform-admin +
  audit; `event_id` stabile per dedup lato ricevitore.
- **Breaker**: il delta è il solo inspector read-only `state()`; nessun trial
  preso; disciplina `BreakerLease` (#405) intatta.
- **RBAC/tenancy guardrail**: ogni metodo di `GuardrailPolicyService` passa da
  `ensure_principal_team_permission` con `GUARDRAILS_READ/MANAGE` (admin-only,
  esclusi da `MODEL_MANAGER`/auditor); le query repo doppio-filtrano per
  `team_id` (cross-team = 404); `resolve_chain` AND-compone router > model >
  team-wide con mutua esclusività validata.
- **Gestione segreti**: signing secret e segreto webhook per-team
  envelope-encrypted, mai serializzati (solo `has_secret`/`has_alert_webhook_secret`),
  decifrati solo sul call path; nessun endpoint di reveal per le credenziali
  openai_compatible.
- **schema.ts** rigenerato e allineato a `HEAD` (route reliability, guardrails
  con `router_id`, capabilities su create/response/update, per-key budget).
- **Reliability view**: RBAC `USAGE_READ`, filtrata team+router, righe shadow
  escluse, read-only rispetto al trial half-open, nessun divide-by-zero.

## Verified and refuted

- **"`except httpx.ConnectError, httpx.ConnectTimeout:` è un SyntaxError
  bloccante" (proposto HIGH) — REFUTED.** PEP 758 (Python 3.14, richiesto da
  `pyproject.toml`) consente le tuple `except` non parentesizzate; la baseline
  lo prova (2184 test collezionati e passati). Idem `except TypeError,
  ValueError:` in `usage_meter.py:127`. Tripperebbe solo tool pinnati sotto 3.14.
- **"Il budget per-chiave è applicato su ogni path" — REFUTED.** Un primo pass
  billing lo aveva messo tra i clean; il ri-tracciamento mostra che è applicato
  solo ai 4 call site native (vedi ISSUE-037): `_prepare.admit` (`:1118`) e i
  due `admit` di failover omettono `api_key_id`.
- **SSRF via allowlist/deny-list, cross-provider aliasing dei client,
  round-trip del segreto mascherato, key-equivalence della cache (R13

  #393/#394)**: non riproducibili sul tree corrente; le protezioni reggono nei
  layer verificati.

## Category scores

| Category | Score | Rationale |
|---|---:|---|
| Security & tenancy | 6.5/10 | Tenancy/RBAC solide, ma egress non applicato al dispatch (034) e `ALLOWED_HOSTS=*` (043) riaprono classi note |
| Correctness | 6.5/10 | Core corretto; i guardrail sono aggirati su failover/cache/native/stream (035/036/038/042) |
| Async & concurrency | 7.0/10 | Prenotazioni Redis atomiche; leak key-scope (044) e release non fail-safe/non shieldata (045) restano |
| Persistence & transactions | 7.5/10 | Migrazioni lineari e simmetriche; il purge omette una tabella team-scoped (040) |
| Billing / business invariants | 6.5/10 | Decimal e hard-budget solidi, ma il per-key budget è no-op sulla superficie primaria (037) |
| Architecture & maintainability | 7.5/10 | Confini dominio/port puliti; scope PATCH non allargabile (041), marker di conflitto committati (050) |
| Testing | 7.0/10 | 93,13% e nuove regressioni, ma i test benedicono la composizione REDACT rotta e non coprono failover/stream/native guard |
| Operations / production readiness | 7.0/10 | Redis obbligatorio e alert requeue buoni; firma webhook opt-in-by-warning e judge senza timeout (046) |
| Frontend | 7.5/10 | Nessun leak di segreti; la console offre uno scope non applicabile e rende alcuni errori come stati vuoti |

**Overall: 6.8/10** al momento della review. Il delta aggiunge molta superficie
di valore e la base regge, ma la ricorrenza dello stesso pattern — *un controllo
dichiarato non richiamato sul percorso alternativo* — attraversa guardrail,
egress e budget per-chiave e costituisce il grosso dei sette HIGH. La lezione dei
Round 13/14 vale di nuovo: **una garanzia va applicata su ogni percorso che la
può eludere, non solo su quello felice** — failover, cache, endpoint native,
streaming e dispatch sono i cinque percorsi dove qui non lo è.

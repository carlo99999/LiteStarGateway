# Plan 19 — Round 15 remediation

Chiude i 19 finding di [issues/round-15.md](../issues/round-15.md)
(0C · 7H · 6M · 6L), tutti Open. Il tema è unico: **una garanzia dichiarata non
viene applicata su un percorso alternativo** (failover, cache, endpoint native,
streaming, dispatch). La remediation applica lo stesso controllo su ogni
percorso che lo può eludere.

Ground rules (invariati da Plan 16/17): **una PR per slice**; ogni PR porta una
**regressione che fallisce prima del fix**; gate completo prima di ogni PR
(`uv run pytest -q --cov-fail-under=80`, `ruff check`, `ruff format --check`,
`pyrefly check`, `just ui-ci`), più `just test-postgres` e `just migration-check`
per ciò che tocca Alembic, e `just test-redis` per ciò che tocca lo store di
prenotazione. Modalità chirurgica: ogni riga cambiata traccia a un finding.

## Decisioni prese in partenza

Domande emerse in review, chiuse qui per non riaprirle in ogni PR.

- **D1 — il budget per-chiave vale sulla superficie OpenAI.** Slice 7 di Plan 17
  dice "un percorso di ammissione, due policy" senza restrizioni di superficie;
  l'omissione di `api_key_id` è un bug, non uno scoping. Il cap si applica a
  `/v1/chat`, `/v1/responses`, embeddings, images e ai retry di failover.
- **D2 — streaming = block-streaming-when-active.** È il default *safe*
  documentato nel design (`docs/next-steps/guardrails.md` §6): quando è
  configurata una regola RESPONSE bloccante per il modello, lo stream viene
  rifiutato (422) prima del primo chunk. Lo scan incrementale a finestra resta
  opt-in e fuori scope.
- **D3 — la rotazione copia il cap.** Ruotare una chiave è igiene, non un modo
  per azzerare un controllo di spesa: il cap per-chiave viene copiato sul
  successore nella stessa transazione.
- **D4 — purge fixato code-only + data-fix.** Aggiungere `GuardrailRuleModel`
  alla lista di purge non richiede migrazione (cancellazione per `team_id`); una
  migrazione dati sblocca i team già tombstoned/incastrati. Niente cambio a
  `ondelete` (eviterebbe la migrazione ma allargherebbe la superficie di rischio
  su una FK che vogliamo esplicita).
- **D5 — scope PATCH con sentinella UNSET.** `model_id`/`router_id` in
  `UpdateGuardrailRuleRequest` diventano `UNSET | None | UUID`: `UNSET` = non
  toccare, `null` esplicito = azzera (verso team-wide), UUID = imposta; la coppia
  è applicata atomicamente.
- **D6 — egress al dispatch come il webhook.** L'adapter riceve l'`EgressAllowlist`,
  ri-risolve per call, pinna l'IP con Host/SNI e ripiega gli indirizzi risolti in
  `ClientKey` (un rebind invalida il client in cache). Nessuna nuova primitiva:
  riusa `resolve_allowlisted_addresses`.

La firma webhook opt-in-by-warning (Deferred del Round 15) **non** è in questo
piano: è una decisione di retro-compatibilità separata, da valutare come hard
refuse allo startup in un piano a sé.

## Perché quest'ordine

Due file sono il collo di bottiglia — `completion_service.py` e
`usage_meter.py` — toccati da sette finding. Vanno **serializzati sotto un solo
owner** (stessa regola di Plan 17). Tutto il resto è disgiunto e parallelo.
La sola migrazione è la data-fix del purge (S4), quindi nessun conflitto di head
Alembic. Si parte dai fix di sicurezza a file disgiunto (sbloccano subito e non
collidono), poi il cluster hot-path in serie.

### Collision map

| File / area | Slice che lo toccano |
|---|---|
| `application/completion_service.py` | S5, S6, S7 (serial, un owner) |
| `application/usage_meter.py` | S5, S7 |
| `application/guardrails/service.py` | S8 (disgiunto) |
| `application/egress.py` + `infrastructure/llm/openai_adapter.py` | S2 |
| `application/guardrail_policy_service.py` + `web/guardrails/` + `ui/` | S9 |
| `persistence/team_repository.py` + `migrations/` | S4 |
| `config.py` / `app.py` | S3 |
| tutto il resto | disgiunto |

### Tracce

- **Traccia A — hot path, serial, un owner:** S5 → S6 → S7.
- **Traccia B — qualsiasi owner, in parallelo:** S1, S2, S3, S4, S8, S9, S10–S13.

---

## Slice 0 → i quick win (S1)

### S1 — Marker di conflitto + redirect sync (ISSUE-050, ISSUE-051) · LOW · ~0,25 d

Due fix banali a file disgiunti, insieme perché entrambi "igiene di rilascio".

- **050:** risolvere i marker `<<<<<<< / ======= / >>>>>>>` in
  `plans/README.md:53-59`, `plans/18-openai-compatible-provider.md` (18 marker) e
  `docs/next-steps/openai-compatible-provider.md:131-156`, tenendo il contenuto
  post-merge corretto.
- **051:** in `openai_adapter.py:266-272` passare un `http_client` con
  `follow_redirects=False` al costruttore sync (l'async è già safe); dichiarare
  l'invariante esplicitamente in `resilience.py`.
- **Done when:** `git diff --check` pulito su tutto il tree; un test che verifica
  `._client.follow_redirects is False` su entrambi i client
  (`sync` e `async`) dell'adapter openai_compatible.

---

## Traccia B — sicurezza e correttezza a file disgiunto

### S2 — Egress applicato al dispatch (ISSUE-034, ISSUE-048) · HIGH · ~1,5 d

Il finding più grave. L'allowlist va ri-verificata dove parte la connessione.

- Passare l'`EgressAllowlist` in `OpenAICompatibleProviderAdapter`; in
  `_leased_async_client`/`_run`, prima di costruire il client, fare
  `await resolve_allowlisted_addresses(host, port, allowlist)` con host/port da
  `urlsplit(credentials["api_base"])`, e **pinnare** l'IP risolto con Host/SNI
  come `post_to_approved_address`.
- Ripiegare gli indirizzi risolti in `ClientKey` (`openai_adapter.py:274-280`)
  così un rebind invalida il client in cache invece di riusarlo.
- **048:** in `_validate_egress` (`credential_service.py:58-63`) rifiutare
  l'userinfo (`parsed.username`/`parsed.password`), così `user:pass@` non entra
  mai in `ClientKey.endpoint` (loggato in chiaro).
- **Done when:** un target che risolve a un indirizzo allowlisted alla *write* e
  a uno non-allowlisted alla *call* ⇒ la **call** fallisce (non solo la write);
  svuotare `OPENAI_COMPATIBLE_ALLOWED_HOSTS` blocca una credenziale già creata;
  `api_base` con userinfo è rifiutato con 400. `just test-redis` incluso se il
  ClientKey tocca la cache client (in-process, ma copre il rebind).

### S3 — `ALLOWED_HOSTS=*` rifiutato + porta (ISSUE-043) · MEDIUM · ~0,5 d

- `config.py:390-395`: oltre alla non-vuotezza, rifiutare `*` (e le voci
  solo-`*.` senza label) fuori da local con `InsecureConfigurationError`.
- Normalizzare o documentare la porta: o si strippa la porta dall'Host prima del
  match, o si valida che le voci con porta combacino — chiudendo il footgun
  `Host: gw:8443` vs voce `gw`.
- **Done when:** `Settings(environment="production", allowed_hosts=("*",), …)`
  fallisce allo startup nominando la variabile; i test in
  `tests/config/test_allowed_hosts.py` invertono il caso wildcard da
  "accetta tutto" a "rifiutato"; un test con `Host` porta-inclusa passa.

### S4 — Purge completo delle regole guardrail (ISSUE-040) · HIGH · ~1 d

- `team_repository.py:203-218`: aggiungere `GuardrailRuleModel` alla lista dei
  figli cancellati per `team_id` (prima del `DELETE FROM team`), aggiornando il
  commento "every table that carries this team's id".
- ~~**Migrazione dati** (unica del piano): cancellare/riassegnare le
  `guardrail_rule` team-wide dei team già tombstoned rimasti incastrati.~~
  **Non serve, e non è stata scritta.** Il fix del codice sblocca da sé i team
  già tombstoned: il purge ora cancella le regole e completa. Non esistono righe
  orfane da riparare, proprio perché la FK era RESTRICT — nessun team è mai
  stato cancellato mentre le sue regole esistevano. Quindi **il piano non ha
  migrazioni**, e non c'è contesa di head Alembic da gestire.
- **Scoperta durante la fix:** il difetto non colpiva solo il purge. Anche la
  `delete_team` ordinaria passa dalla stessa `delete()`, quindi un team **senza
  storico di fatturazione, senza modelli e senza chiavi** rispondeva 409 "team
  not empty" — nominando una condizione che l'operatore non poteva trovare,
  perché le regole guardrail non fanno parte di ciò che quel messaggio descrive.
  Coperto da una seconda regressione.
- **Done when:** creare una regola team-wide → `delete_team` (tombstone) →
  `purge_team` **completa** senza 409, e nessuna `guardrail_rule` resta per quel
  team; più il caso hard-delete; regressioni in
  `tests/teams/test_retention_lifecycle.py` che falliscono prima del fix.

### S8 — REDACT compone in sequenza (ISSUE-039) · HIGH · ~0,75 d

- `guardrails/service.py:72-89`: girare i provider concorrenti **solo** per il
  rilevamento BLOCK (any-BLOCK-wins resta), poi ri-eseguire i provider
  REDACT-capable **in sequenza** nell'ordine di catena, ciascuno sul testo
  accumulato dal precedente.
- Riscrivere `test_redactions_compose_in_chain_order` con redattori
  *input-dependent* (A maschera email, B maschera carte) così il test cattura
  davvero la composizione.
- **Done when:** con A+B in catena, l'output non contiene né email né carta;
  il test fallisce sulla semantica last-writer-wins attuale.

### S9 — Scope PATCH + unicità nome (ISSUE-041, ISSUE-049) · MEDIUM/LOW · ~1 d

- **041:** sentinella `UNSET` per `model_id`/`router_id` in
  `UpdateGuardrailRuleRequest` (`web/guardrails/schemas.py:96-101`);
  `guardrail_policy_service.update_rule:119` applica il null esplicito come
  "azzera" e la coppia scope atomicamente; UI (`ruleForm.ts`/`GuardrailsPage.tsx`)
  invia UNSET quando il campo non cambia, `disabled` sul select `kind` in edit.
- **049:** in `guardrail_repository.add` catturare l'`IntegrityError` su
  `uq_guardrail_rule_team_id` e mapparlo a `InvalidGuardrailRule` (400/409),
  come i repo fratelli.
- **Done when:** PATCH scoped→team-wide allarga davvero (200 + scope cambiato);
  model→router commuta senza 400; due create concorrenti con lo stesso nome danno
  400/409, non 500. Rigenerare `schema.ts` (`just ui-schema`).

### S10 — Judge con time budget (ISSUE-046) · MEDIUM · ~0,5 d

- `guardrail_config.py`: aggiungere `timeout_ms` ai `_JUDGE_KEYS` (default
  ~2000 ms, bounded come il webhook); `judge.py:89-90`: `asyncio.timeout` attorno
  alla `complete`, risolvendo il timeout secondo la fail policy.
- **Done when:** un judge-model che stalla oltre `timeout_ms` produce BLOCK
  (`fail: closed`) o ALLOW+warning (`open`) entro il budget, non l'attesa dei 60 s
  del gateway.

### S11 — Segreto webhook per-team: vuoto e clear (ISSUE-047) · LOW · ~0,5 d

- `web/teams/schemas.py:48`: rifiutare stringa vuota/whitespace; introdurre una
  semantica esplicita di clear (flag o sentinella) distinta da "ometti = mantieni"
  in `budget_repository._upsert` e `channel_resolver`.
- **Done when:** PUT `""` è 400; esiste un modo di azzerare il segreto per-team
  senza cancellare il budget; `has_alert_webhook_secret` non è mai `true` con un
  segreto vuoto sottostante.

### S12 — Rotazione copia il cap (ISSUE-052) · LOW · ~0,5 d

- `teams/controller.py:501-528` (rotate): copiare la riga `api_key_budget` sul
  nuovo `api_key_id` nella stessa transazione (mantenendo finestra/mode/limite).
- **Done when:** ruotare una chiave capata lascia la nuova chiave capata con lo
  stesso limite; regressione end-to-end.

---

## Traccia A — hot path, serial, un owner

Ordine: **S5 → S6 → S7**. Ogni PR è piccola e verificabile; il file è lo stesso,
quindi mai due branch vive insieme qui.

### S5 — Guardrail su failover e cache (ISSUE-035, ISSUE-036) · HIGH · ~1,5 d

- **035:** in `_chat_completion_with_failover` derivare i tentativi da `clean`
  (redatto) invece di `sanitized` — solo clamp/validate restano per-modello
  (`:1341-1344` e il gemello stream `~:1594`) — e passare `router_id=router.id`
  alla `_dispatch` di failover (`:1356-1367`) così anche le regole RESPONSE
  scoped-per-router scattano.
- **036:** spostare `_cache_put`/`_semantic_put` (`:426-437`) **dopo**
  `_guard_response` (`:444`) — si cache-a il body screenato — ed eseguire
  `_guard_response` anche sulle hit (`:376-389` e replay stream `:1525-1531`);
  in alternativa non cache-are quando la chain RESPONSE è non vuota.
- **Done when:** REDACT + failover ⇒ il candidato #2 riceve il body redatto;
  BLOCK/REDACT RESPONSE + cache ⇒ la seconda richiesta identica è
  bloccata/redatta e nessun body pre-guardrail resta in cache. Regressioni sui
  path failover e cache-hit (assenti oggi).

### S6 — Native e streaming (ISSUE-038, ISSUE-042) · HIGH/MEDIUM · ~1,5 d

- **038:** cablare `_guard_request` in `prepare_native` (`:745-781`) con
  estrattori di testo native; estendere `payloads.response_text` (`:41-52`) alle
  forme Anthropic (`content` blocks) e Gemini (`candidates`), così il guard di
  risposta non giudica `""`.
- **042:** all'apertura di ogni stream (`open_chat_stream:1423`,
  `open_responses_stream:1665`, i due native) risolvere la chain RESPONSE e
  rifiutare lo stream (422) se non vuota (D2), senza mai ritirare chunk già
  emessi.
- **Done when:** un prompt bloccato su `/v1/messages` è rifiutato come su
  `/v1/chat/completions`; una regola RESPONSE bloccante fa 422 su `stream=true`
  invece di passare; test per entrambe le forme native e per lo stream.

### S7 — Billing su ogni ammissione + release fail-safe (ISSUE-037, ISSUE-044, ISSUE-045) · HIGH/MEDIUM · ~1,5 d

- **037:** passare `api_key_id` in `_prepare.admit` (`:1118`) e nei due `admit`
  di failover (`:1348`, `:1601`) — è già in scope in entrambe le funzioni.
- **044:** in `UsageMeter.admit` (`:427-430`) avvolgere `_admit_team` in
  try/except che rilascia `key_claim` prima di rilanciare.
- **045:** rendere `UsageMeter.release` best-effort (log + continua sul resto
  delle reservation, mai propagare a un successo già settlato) e shieldare il
  `finally` di `_metered_cache_hit_stream` (`:1525-1542`) come il gemello
  `metered_stream`.
- **Done when:** cap per-chiave block superato ⇒ `/v1/chat/completions` è
  rifiutato; un rifiuto team non lascia spend fantasma nello scope key
  (`reserved(key_scope)==0`); un errore Redis in release non trasforma un
  successo in 500 né perde il settlement dello stream. `just test-redis` +
  `just test-postgres`.

---

## Riepilogo sequenza

| Slice | Finding | Sev | Traccia | Migr. | Stima |
|---|---|---|---|---|---|
| S1 | 050, 051 | L | B | no | 0,25 d |
| S2 | 034, 048 | H/L | B | no | 1,5 d |
| S3 | 043 | M | B | no | 0,5 d |
| S4 | 040 | H | B | no¹ | 1,0 d |
| S8 | 039 | H | B | no | 0,75 d |
| S9 | 041, 049 | M/L | B | no | 1,0 d |
| S10 | 046 | M | B | no | 0,5 d |
| S11 | 047 | L | B | no | 0,5 d |
| S12 | 052 | L | B | no | 0,5 d |
| S5 | 035, 036 | H | A | no | 1,5 d |
| S6 | 038, 042 | H/M | A | no | 1,5 d |
| S7 | 037, 044, 045 | H/M | A | no | 1,5 d |

¹ La migrazione prevista per S4 non è servita: il fix del codice sblocca da sé i
team già tombstoned, e la FK RESTRICT garantisce che non esistano righe orfane
da riparare. **Il piano non ha migrazioni**, quindi nessuna contesa di head
Alembic.

Critical path = Traccia A (S5→S6→S7, ~4,5 d serial); la Traccia B gira in
parallelo. Con un owner sul hot path e uno sul resto, ~11 giorni-persona di
lavoro chiudono in ~6-7 giorni di calendario.

**Chiusura:** un round di regressione (come Round 14 dopo Round 13) che
ri-verifica i 19 finding sul tree post-remediation, con particolare attenzione
agli interleaving stale/stream/failover che le regressioni di questo piano
introducono.

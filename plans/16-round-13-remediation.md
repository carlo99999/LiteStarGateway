# Plan 16 — Round 13 remediation

Piano di esecuzione per chiudere tutti i finding di
[issues/round-13.md](../issues/round-13.md): **2 HIGH + 7 MEDIUM** (ISSUE-022…030).

> **Stato: completato.** Tutte le PR sono state mergiate — #392 (022), #393 e
> #394 (023), #400 (024), #395 (025), #396 (026), #397 (027), #398 (028), #399
> (029), #401 (030). Scostamenti dal piano, documentati nelle rispettive PR:
> il claim dell'outbox usa un compare-and-swap portabile invece di
> `FOR UPDATE SKIP LOCKED` (funziona identico su PostgreSQL e SQLite); la PR 4
> non aggiunge il purge esplicito del namespace a revoca chiave, perché le
> entry di una chiave revocata sono già irraggiungibili e ora limitate; la
> PR 8 non introduce l'allowed-hosts middleware, che è una modifica di
> deployment più ampia del finding.

Regole del round:

- una PR per issue (le due HIGH sono split in due PR dove il blast radius lo
  richiede), ogni PR con regressione che **fallisce prima** della fix;
- ogni PR aggiorna la riga corrispondente nella tabella di `round-13.md`
  (`Open` → `Fixed by #NNN`);
- gate per merge: `uv run pytest -q --cov=src/litestar_gateway --cov-fail-under=80`,
  `uv run ruff check`, `uv run ruff format --check`, `uv run pyrefly check`,
  `uv run pre-commit run --all-files`, e per le PR con migrazione anche
  `just test-postgres` + `just migration-check`.

## Sequenza e dipendenze

| # | PR | Issue | Sev | Migrazione | Dipende da |
|---|----|-------|-----|-----------|-----------|
| 1 | `fix/negative-model-pricing` | ISSUE-022 | HIGH | sì (CHECK) | — |
| 2 | `fix/exact-cache-key-fingerprint` | ISSUE-023a | HIGH | no | — |
| 3 | `fix/semantic-cache-namespace` | ISSUE-023b | HIGH | no | PR 2 |
| 4 | `fix/semantic-cache-global-bound` | ISSUE-024 | MED | no | PR 3 |
| 5 | `fix/client-registry-generation-loss` | ISSUE-025 | MED | no | — |
| 6 | `fix/budget-alert-outbox-atomicity` | ISSUE-026 | MED | sì (claim) | — |
| 7 | `fix/failover-overall-deadline` | ISSUE-027 | MED | no | — |
| 8 | `fix/sso-db-redirect-policy` | ISSUE-028 | MED | no | — |
| 9 | `fix/redis-breaker-half-open` | ISSUE-029 | MED | no | — |
| 10 | `fix/team-purge-completeness` | ISSUE-030 | MED | no | PR 6 (nuove colonne outbox) |
| 11 | `docs/round-13-remediated` | — | — | no | tutte |

Solo PR 1 e PR 6 toccano Alembic: vanno mergiate **non in parallelo tra loro**
per evitare due head. Tutto il resto è parallelizzabile in tre track:
cache (2→3→4), concorrenza (5, 6→10, 9), contratti (7, 8).

Ordine consigliato di merge: **1 → 2 → 3 → 6 → 7 → 9 → 5 → 8 → 4 → 10 → 11**
(prima le due invarianti economiche/di correttezza, poi i protocolli
concorrenti, poi i tail).

---

## PR 1 — ISSUE-022: rifiutare tariffe negative/non finite (HIGH)

**Causa.** `CreateModelRequest`/`UpdateModelRequest`
(`infrastructure/web/models/schemas.py:27-76`) accettano `float` liberi;
`application/model_service.py` non valida né in create né in update; le colonne
in `persistence/orm.py:783-797` non hanno CHECK; `domain/pricing.py:79-94`
moltiplica quello che riceve → `compute_cost` negativo → credito nel ledger e
budget hard aggirabile da un `MODEL_MANAGER`.

**Fix.**

1. Nuova funzione dominio `domain/pricing.py::validate_rate_card(...)` (o
   `validate_rates`), unico punto di verità: ogni tariffa e ogni valore di
   `image_prices` dev'essere `None` oppure un float **finito e >= 0**
   (rifiuta `NaN`, `±Infinity`, `bool`), e le chiavi di `image_prices` devono
   passare il formato `size/quality`.
2. Chiamarla in `ModelService.create()` (`:74-143`) e `ModelService.update()`
   (`:224-240`) — non nel controller, così la regola vale anche per bootstrap,
   seed e futuri caller.
3. Nuova eccezione dominio (es. `InvalidModelPricing`) mappata a **422** nel
   solito exception handler web, coerente con gli altri errori di validazione
   modello.
4. Migrazione Alembic: `CHECK (col IS NULL OR col >= 0)` sulle quattro colonne
   scalari. `image_prices` è JSON: nessun CHECK portabile, resta coperto dalla
   validazione applicativa (documentarlo nel docstring della migrazione).

**Test.**

- unit `tests/.../test_pricing.py`: matrice di valori (negativo, `-0.0`, `0`,
  `NaN`, `inf`, valore valido) su ogni dimensione + `image_prices`;
- HTTP create e update come `MODEL_MANAGER`: 422 su ogni dimensione negativa,
  201/200 su zero esplicito e su update parziale che non tocca il pricing;
- regressione end-to-end: budget `$1`, modello con tariffe negative rifiutato in
  create → impossibile riprodurre `spent=-39.0`;
- test migrazione: insert diretta negativa respinta su Postgres.

**Rischio.** Un modello già in DB con tariffa negativa farebbe fallire l'upgrade
della migrazione. Aggiungere nello stesso revision uno step di normalizzazione
(clamp a `0.0` + log WARNING) **prima** di creare il constraint.

---

## PR 2 — ISSUE-023a: fingerprint della request effettiva nella exact cache (HIGH)

**Causa.** `domain/response_cache_key.py:22-58` costruisce la chiave da
`canonical_model_name` + una `_KEY_FIELDS` parziale, e
`completion_service.py::_cache_key_for` passa `clean` (la request pre-merge di
`params`/`params_enforced`). Mancano `model.id`, l'operazione, la revisione di
configurazione, e campi behavior-affecting (`n`, `parallel_tool_calls`,
penalty, opzioni reasoning, `logit_bias`, `logprobs`, `top_logprobs`,
`modalities`, `verbosity`/`reasoning`).

**Fix.**

1. `derive_cache_key(...)` prende in più `model_id: UUID`, `operation: str` e
   `config_fingerprint: str`, e li inserisce nel dict canonico insieme a
   `provider`, `provider_model_id`, `params` e `params_enforced` effettivi.
2. Passare da una allow-list a una **deny-list**: la chiave copre tutta la
   request eccetto i campi esplicitamente non-determinanti (`stream`, `user`,
   `stream_options`, `metadata`). Così un campo nuovo è cacheable-safe per
   default invece che silenziosamente ignorato.
3. Prefissare il digest con una versione di schema (`v2:`) così il cambio di
   derivazione invalida naturalmente le entry vecchie senza flush manuale.
4. `_cache_key_for` deve ricevere la request **dopo** il merge di
   `params`/`params_enforced` (stesso payload che va al provider), non `clean`.

**Test.**

- table-driven: `n`, `parallel_tool_calls`, penalty, reasoning, `logit_bias`
  producono chiavi diverse; `stream`/`user` no;
- cross-model: stesso body, `model.id` diverso → chiavi diverse;
- chat vs `responses` con stesso testo → chiavi diverse;
- update del modello (params_enforced / provider_model_id) → chiave diversa;
- delete+recreate con lo stesso nome → chiave diversa (è il caso che `model.id`
  chiude).

---

## PR 3 — ISSUE-023b: namespace e verifica esatta nel semantic tier (HIGH)

**Causa.** Il protocollo `SemanticResponseCache`
(`domain/ports/response_cache.py:63-91`) e l'adapter
(`infrastructure/cache/semantic.py:27-87`) sono namespaced solo su
`(team_id, api_key_id)`; la vista semantica
(`domain/response_cache_semantic.py:29-62`) considera solo `messages`/`input`
e ignora modello, operazione, `instructions`, tools, tool choice e formato di
output. Con similarità `1.0` una request Responses su modello B riceve la
risposta chat del modello A.

**Fix.**

1. Estendere `find`/`add` con un `scope` esplicito
   (`team_id, api_key_id, model_id, operation, config_fingerprint`) — riusare
   la stessa fingerprint di PR 2 come componente del bucket key.
2. Salvare accanto al vettore una **hash dei campi non semantici**
   (`instructions`, `tools`, `tool_choice`, `response_format`, `n`, params
   enforced) e rifiutare un nearest-neighbor hit se la hash non coincide: la
   similarità decide solo sul testo, non sul contratto.
3. Aggiornare i due call site in `completion_service.py:975-987,1157-1180` e la
   contabilizzazione dell'hit (`:338-355,464-516`) perché il modello attribuito
   sia per costruzione quello dell'entry.

**Test.** Cross-model, chat-vs-Responses, tools/tool_choice/instructions
diversi, `response_format` diverso: tutti **miss**. Stesso scope e stesso testo:
hit. Più il test già riprodotto nel round (`model-a / A_ONLY`, vettore `[1,0]`)
come regressione negativa.

---

## PR 4 — ISSUE-024: bound globale della semantic cache (MEDIUM)

**Causa.** `infrastructure/cache/semantic.py:23-88`: 50 entry **per bucket** ma
nessun tetto sul numero di bucket; `find()` fa sweep solo del bucket letto,
`add()` non fa sweep globale → 1000 API key = 1000 bucket residenti anche dopo
il TTL.

**Fix.**

1. `MAX_BUCKETS` (e/o `MAX_TOTAL_ENTRIES`) con eviction LRU sull'`OrderedDict`
   già presente — `move_to_end` c'è, manca solo il `popitem(last=False)`.
2. Sweep opportunistico cross-bucket in `add()`: a ogni inserimento controlla
   un numero costante di bucket più vecchi e rimuove quelli interamente scaduti
   (costo O(1) ammortizzato, niente task di background).
3. Purge esplicito del namespace su revoca/rotazione della API key e su delete
   del team: nuovo metodo `invalidate(team_id, api_key_id=None)` sul protocollo,
   chiamato dai service di revoca/rotazione.

**Test.** 10k key → conteggio bucket limitato; expiry senza revisit → i bucket
scaduti spariscono; revoca chiave → namespace vuoto; concorrenza add/find.

---

## PR 5 — ISSUE-025: il registry non deve perdere la generazione sostitutiva (MEDIUM)

**Causa.** `_close_entry_locked()`
(`infrastructure/llm/client_registry.py:186-190,231-240`) fa `pop(key)` senza
confrontare l'identità: quando A1 (closing) viene rilasciata, rimuove A2 dal
dizionario pur chiudendo solo A1. A2 resta usabile dal lease corrente ma esce
dal lifecycle e non viene chiusa da `aclose()`.

**Fix.**

1. In `_close_entry_locked` rimuovere lo slot **solo** se
   `self._entries.get(key) is entry`.
2. Tenere le generazioni ritirate in un set `_retired: set[_Entry]` finché
   l'ultimo lease non le chiude, e includerlo in `aclose()` (`:248-268`) così
   lo shutdown chiude tutto esattamente una volta.
3. Verificare che il conteggio capacità in `_evict_locked` non conti due volte
   le retired.

**Test.** La sequenza del round: `capacity=1`, lease A1 → lease B (marca A1
closing) → nuovo lease A crea A2 → release A1 → assert A2 ancora in `_entries`,
A1 chiusa una sola volta; poi `aclose()` → `a2.closed is True`.

---

## PR 6 — ISSUE-026: outbox alert atomico + claim del dispatcher (MEDIUM)

**Causa.** `application/usage_meter.py:761-812` chiama `record_fired()` e poi
`enqueue_alert()`, e il repository committa **due volte**
(`budget_alert_state_repository.py:46-87`): un crash in mezzo lascia una soglia
"fired" senza alert e le valutazioni successive la saltano per sempre. Il drain
(`:112-169`) seleziona, invia e poi cancella **senza claim**: due repliche
inviano lo stesso alert.

**Fix.**

1. Un solo metodo `record_fired_and_enqueue(...)` che fa i due insert nella
   **stessa transazione**, con un unico commit; `usage_meter` chiama quello e
   il commento a `:765-769` torna vero. Mantenere la gestione `IntegrityError`
   sul dedup key (rollback → `None`, nessun enqueue).
2. Claim atomico e recuperabile nel drain: nuove colonne
   `claimed_at`/`claimed_until`/`claimed_by` su `pending_budget_alert`
   (migrazione Alembic). Su Postgres selezione con
   `FOR UPDATE SKIP LOCKED` + set del lease; su SQLite `UPDATE ... WHERE id IN
   (SELECT ... ) AND (claimed_until IS NULL OR claimed_until < now)` seguito da
   `SELECT` dei claimati (single-writer, quindi sufficiente). Un lease scaduto
   torna eleggibile → nessuna riga bloccata da un worker morto.
3. Correggere il commento fuorviante in
   `infrastructure/budget_alert_reconciler.py:12-15`.

**Test.** Fault injection tra dedup e outbox (adapter che solleva
sull'enqueue) → `durable_fired_count == 0` e retry alla valutazione successiva;
test concorrente multi-session su Postgres con barriera nel canale → un solo
send, `delivered` totale `1`; lease scaduto → riga ripresa.

**Deferred esplicito.** La duplicazione multi-canale
(`:128-134`) resta at-least-once by design: documentarlo nella docstring, non
è in scope.

---

## PR 7 — ISSUE-027: `overall_deadline_ms` come budget wall-clock reale (MEDIUM)

**Causa.** La deadline è controllata solo **prima** del tentativo successivo
(`application/completion_service.py:1082-1103` e `:1321-1342`); le await lunghe
a `:1119-1130` e `:1358-1386` non sono limitate. Con deadline 10 ms e primary
lento 120 ms la chiamata è tornata con successo a 122 ms.

**Fix.**

1. Calcolare all'ingresso della chain una `deadline_at = monotonic() + budget`
   e derivare il residuo prima di ogni dispatch.
2. Avvolgere ogni dispatch (non-streaming) e l'`open + prime` dello stream in
   `asyncio.timeout(remaining)`; residuo `<= 0` ⇒ non si parte.
3. Definire l'errore esposto alla scadenza (riusare l'errore di timeout/failover
   già mappato, non introdurne uno nuovo) e verificare che release della
   reservation, settlement e bookkeeping del breaker avvengano comunque sul
   percorso di cancellation.
4. Allineare `docs/next-steps/cross-provider-failover.md:90-97` al comportamento
   effettivo.

**Test.** Successo lento oltre deadline → errore, non 200; errore lento; retry
che parte appena sotto il limite; cancellation — per entrambi i percorsi
(non-streaming e streaming). Sostituire la deadline illegale `-1` in
`tests/budgets/test_failover_orchestration.py:349-364` con valori realistici.

---

## PR 8 — ISSUE-028: SSO DB-backed senza redirect fisso rifiutato fuori local (MEDIUM)

**Causa.** `config.py:369-378` blocca il caso env, ma
`application/sso_settings_service.py:37-75` valida discovery/client id/secret e
**non** `redirect_uri`; il fallback in `infrastructure/web/session/sso.py:98-103`
torna a derivare la callback da `request.base_url`. La console invia `null`
(`ui/src/features/sso/SsoSettingsPage.tsx:70-83,198-208`).

**Fix.**

1. Iniettare la policy di deployment (il flag `is_local` già derivato in
   `Settings.from_env`) in `SsoSettingsService` e rifiutare con
   `InvalidSsoSettings` una `upsert(enabled=True, redirect_uri=None)` fuori
   local — stesso messaggio del gate env, così i due percorsi coincidono.
2. UI: `redirect_uri` **required condizionalmente** quando `enabled` è on, con
   messaggio di errore inline.
3. Difesa in profondità: allowed-hosts middleware con gli host attesi, così il
   fallback locale non può essere pilotato da un `Host` forgiato.

**Test.** `ENVIRONMENT=production` + upsert senza redirect → 422; ambiente local
→ accettato; request con `Host: attacker.example` → nessuna redirect derivata
dall'host in production; hot reload della configurazione DB.

---

## PR 9 — ISSUE-029: half-open single-trial nel breaker Redis (MEDIUM)

**Causa.** `infrastructure/circuit_breaker.py:141-164` scrive `cb:opened:*` con
`ex = cooldown`: allo scadere del cooldown la chiave sparisce, e `allow()`
(`:128-139`) legge `None` e ritorna `True` per tutti — il ramo half-open non
viene mai raggiunto e `cb:trial:*` non viene creato. Il `FakeRedis` dei test
(`tests/routing/test_circuit_breaker.py:22-53`) registra i TTL ma non li applica,
quindi il test resta verde.

**Fix.**

1. Far sopravvivere lo stato open oltre il cooldown: TTL del marker
   `opened` = `cooldown * K` (K ≥ 2, configurabile) oppure nessun TTL con delete
   esplicita su trial-success.
2. Rendere atomica la transizione open→half-open con uno **script Lua**: legge
   `opened`, se il cooldown è scaduto fa `SET trial NX` e ritorna il verdetto in
   una singola round-trip. Elimina anche la race read-then-set attuale.
3. Rendere il `FakeRedis` TTL-faithful (clock iniettabile + scadenza applicata
   in `get`), altrimenti la regressione non fallisce prima della fix.

**Test.** Con fake TTL-faithful: dopo il cooldown, N `allow()` concorrenti →
esattamente **un** `True`; trial success → breaker closed e marker puliti; trial
failure → riaperto con cooldown fresco. Più il test opzionale marcato contro
Redis reale, dietro lo stesso marker già usato per i test Postgres.

---

## PR 10 — ISSUE-030: purge del team completo (MEDIUM)

**Causa.** `persistence/team_repository.py:186-195` elimina solo sette figli.
Mancano `BudgetAlertStateModel` e `PendingBudgetAlertModel` (FK RESTRICT verso
`team.id`, `orm.py:623-679`), i grant **ricevuti** — `RouterGrantModel.team_id`
(`orm.py:319-337`) e `ModelGrantRecord.team_id` (`:827-852`) — e
`RoutingDecisionModel` (`:352-399`), che non ha FK ma conserva `team_id`,
`user_text` e `system_prompt`. L'`IntegrityError` diventa `TeamNotEmpty` → 409:
un team che ha mai superato una soglia non è purgabile.

**Fix.**

1. Aggiungere alla lista, nell'ordine FK corretto (figli outbox prima del dedup
   ledger): `PendingBudgetAlertModel`, `BudgetAlertStateModel`,
   `RouterGrantModel` (per `team_id`), `ModelGrantRecord` (per `team_id`),
   `RoutingDecisionModel`.
2. Documentare nel docstring cosa **per policy sopravvive** (audit del purge,
   record di piattaforma) e perché — è il contratto dell'endpoint.
3. Verificare che tutto resti nella stessa unit of work di
   `application/team_service.py:365-389`.

**Test.** Estendere `tests/teams/test_retention_lifecycle.py:195-230`: team
tombstonato con fired state + pending alert + grant router + grant modello + una
routing decision con prompt → `204`, tutte le righe team-scoped assenti,
audit del purge presente. Ripetere su Postgres con FK reali.

---

## PR 11 — docs: chiusura del round

- `issues/round-13.md`: tutte le righe `Open` → `Fixed by #NNN`, e aggiornare
  la sezione *Resolution status*;
- `issues/INDEX.md`: riga Round 13 con `0·2·7·0` e stato, più il nuovo overall;
- `plans/README.md`: snapshot dello stato e link a questo piano;
- `docs/next-steps/cross-provider-failover.md` è già aggiornato da PR 7.

## Rischi trasversali

- **Invalidazione cache (PR 2/3).** Il cambio di derivazione svuota di fatto
  exact e semantic tier: atteso, va detto nella PR description. Il prefisso `v2:`
  evita hit su entry vecchie con semantica diversa.
- **Due migrazioni (PR 1, PR 6).** Merge sequenziale, `just migration-check`
  dopo ciascuna.
- **Fake troppo ottimistici.** PR 6 e PR 9 valgono solo se i test girano contro
  un backing store fedele (Postgres reale / fake TTL-faithful). È il tema di
  fondo del round: senza quello la fix non è verificata.

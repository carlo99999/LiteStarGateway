# Code Review — Round 14 (2026-07-28)

[← Index](INDEX.md)

Review di regressione dell'intero tree corrente (`d4db533`), con focus sulla
remediation del Round 13 (#392–#401). Due reviewer indipendenti hanno coperto
in parallelo pricing/cache/client lifecycle e outbox/deadline/SSO/breaker/purge;
il coordinatore ha verificato le riproduzioni e lo storico. Sono inclusi solo
difetti riproducibili o deterministici sul tree corrente.

Baseline eseguita:

- `uv run pytest -q --cov=src/litestar_gateway --cov-report=term
  --cov-fail-under=80`: **1876 passed, 6 skipped**, copertura **92,94%**;
- `just test-postgres` (PostgreSQL 17 effimero e catena Alembic completa):
  **1882 passed**;
- test mirati di remediation: **185 passati** (115 cache/pricing/registry + 70
  outbox/deadline/SSO/breaker/purge);
- `uv run ruff check`, `uv run ruff format --check` e `uv run pyrefly check`:
  verdi;
- `uv run pip-audit`: nessuna vulnerabilità nota; il package locale non
  pubblicato è l'unico skip;
- `git diff --check` e `uv run rumdl check issues/round-13.md`: verdi.

## Executive summary

Le nove remediation del Round 13 chiudono correttamente i due HIGH e la
maggior parte dei MEDIUM: pricing non finito/negativo, equivalenza cache,
bound della cache semantica, generazioni del client registry, atomicità
dedup+outbox, deadline, nuovo SSO DB, marker Redis e purge sono tutti coperti
da nuove regressioni e hanno retto la verifica mirata.

Restano però tre MEDIUM nei confini di transizione: un alert privo di canale
viene comunque leased per cinque minuti, le configurazioni SSO DB legacy senza
callback non sono rese sicure, e il breaker non associa l'esito al trial che
lo ha ottenuto. Sono difetti più stretti del Round 13, ma impediscono di
considerare le remediation totalmente chiuse.

Counts: **0 CRITICAL · 0 HIGH · 3 MEDIUM · 0 LOW**.

## Issue summary

| ID | Title | Severity | Files | Status |
|---|---|---|---|---|
| ISSUE-031 | Un alert senza canale resta claimed per 300 secondi | MEDIUM | `budget_alert_state_repository.py` | Fixed by #403 |
| ISSUE-032 | Una configurazione SSO DB legacy senza redirect URI riapre il fallback Host | MEDIUM | `sso_settings_service.py`; `sso/dynamic.py`; `session/sso.py` | Fixed by #404 |
| ISSUE-033 | Esiti stale possono chiudere o riaprire un half-open trial non loro | MEDIUM | `circuit_breaker.py`; `ports/circuit_breaker.py` | Fixed by #405 |

## Findings

### ISSUE-031 — Un alert senza canale resta claimed per 300 secondi (MEDIUM)

**Dove.** `src/litestar_gateway/infrastructure/persistence/budget_alert_state_repository.py:202-213`
esegue `_claim()` prima di `resolve_channels()`; il compare-and-swap persiste
`claimed_until=now+300s` a `:224-258`. Il test
`tests/budgets/test_budget_alert_dispatch.py:114-121` controlla soltanto che la
riga esista, non che sia rimasta unclaimed.

**Problema.** Quando non esistono canali, il dispatcher fa `continue`, ma non
rilascia il claim. La riga non è fallita né consegnata, però un canale aggiunto
subito dopo non la può selezionare fino alla scadenza del lease.

**Perché è un problema.** Il contratto documentato del caso no-channel è
"untouched, waiting for configuration". Invece il primo worker introduce un
blackout di consegna fino a cinque minuti per ogni alert già in coda; non è un
errore transitorio del canale e non incrementa attempts, quindi non dà nemmeno
un segnale operativo.

**Impatto verificato.** Con una pending row e resolver `[]`,
`dispatch_pending()` restituisce `0`, `attempts=0`, ma
`claimed_until` è a 300 secondi nel futuro. Il secondo dispatcher non può
consegnare la riga finché il lease non scade. Classificazione: **Confirmed**.

**Correzione suggerita.** Risolvere i canali prima di claimare, oppure azzerare
`claimed_until` in una transazione prima del `continue`. Estendere il test
no-channel con l'asserzione `claimed_until is None` e con un secondo drain dopo
l'aggiunta del canale.

### ISSUE-032 — Una configurazione SSO DB legacy senza redirect URI riapre il fallback Host (MEDIUM)

**Dove.** La nuova validazione è soltanto nel write path
`src/litestar_gateway/application/sso_settings_service.py:64-77`. Il resolver
runtime carica qualunque DB row enabled in
`infrastructure/sso/dynamic.py:90-108`, incluso `redirect_uri=None`; il login
deriva poi la callback da `request.base_url` in
`infrastructure/web/session/sso.py:98-103`. La migrazione originaria consente
il NULL (`migrations/versions/2026-07-23_add_sso_settings_7c325e18ff38.py:25-47`)
e nessuna migrazione/backstop neutralizza le righe già esistenti.

**Problema.** #398 blocca future upsert fuori local, ma non una configurazione
DB enabled creata prima della fix. Quella riga resta selezionata dal resolver e
riapre esattamente il fallback Host che ISSUE-028 voleva eliminare.

**Perché è un problema.** Un deployment aggiornato può credere di aver ricevuto
la protezione senza alcuna azione dell'operatore, ma continuare a dichiarare
all'IdP una callback controllabile tramite Host. State, nonce e PKCE riducono
l'impatto, ma non eliminano steering/DoS e il rischio con matching IdP
non-esatto.

**Impatto verificato.** La traccia è deterministica: una row legacy
`enabled=True, redirect_uri=None` soddisfa `dynamic.py:93-107`, quindi
`_redirect_uri()` restituisce `https://<Host>/sso/callback`. Non esiste un
test/migrazione che renda quella row invalida o disabled. Classificazione:
**Confirmed**.

**Correzione suggerita.** Aggiungere una migrazione che disabiliti o segnali le
row enabled senza redirect nei deployment non-local e un backstop runtime nel
resolver che rifiuti quella configurazione. Coprire upgrade da row legacy e
login con Host forgiato dopo l'upgrade.

### ISSUE-033 — Esiti stale possono chiudere o riaprire un half-open trial non loro (MEDIUM)

**Dove.** Il port non restituisce alcun token di ownership:
`src/litestar_gateway/domain/ports/circuit_breaker.py:15-17`. L'adapter
in-memory considera qualsiasi successo durante `half_open` come successo del
trial (`infrastructure/circuit_breaker.py:58-96`). L'adapter Redis crea un
marker globale in `:145-159`, poi `record_success()` e `record_failure()`
deducono l'ownership dalla sola presenza di quel marker (`:161-195`).

**Problema.** Una richiesta A ammessa prima dell'apertura può terminare dopo
che la richiesta T ha ottenuto il trial half-open. L'esito positivo di A
cancella marker open e marker trial; l'esito negativo di A può invece
riaprire il breaker. Nessun dato distingue A da T.

**Perché è un problema.** Il requisito del breaker è esattamente che il solo
trial decida la transizione. Un esito stale può rimuovere il gate mentre T è
ancora in corso, e i caller successivi passano tutti: il precedente stampede
Redis ritorna in una diversa interleaving. La stessa ambiguità esiste nel
breaker in-memory, non solo fra repliche Redis.

**Impatto verificato.** Con Redis fake TTL-faithful: aprire `m`, avanzare oltre
cooldown, `allow(m)` concede T; `record_success(m)` da A svuota le chiavi; cinque
`allow(m)` successivi restituiscono tutti `True` mentre T non ha concluso. I
test correnti coprono solo l'ordine in cui l'esito appartiene implicitamente al
trial. Classificazione: **Confirmed**.

**Correzione suggerita.** Far restituire da `allow()` un lease/token opaco e
richiederlo in `record_success`/`record_failure`, oppure implementare uno state
machine atomico che associa esplicitamente outcome e claim. Aggiungere i casi
stale-success e stale-failure per entrambi gli adapter.

## Resolution status

- ISSUE-022–ISSUE-030: le remediation principali sono verificate sul tree
  corrente; ISSUE-031–033 sono nuovi tail della rispettiva area e non duplicati
  letterali dei finding chiusi.
- ISSUE-031–ISSUE-033: **tutti chiusi**. La review non ha modificato codice
  prodotto; la remediation è arrivata subito dopo, una PR per issue, ognuna con
  una regressione che falliva prima della fix:
  - **ISSUE-031** (#403): il claim resta prima di `resolve_channels` — un
    resolve che solleva deve appartenere a un solo dispatcher, altrimenti due
    incrementerebbero `attempts` per lo stesso errore — e il ramo no-channel
    ora rilascia il lease esplicitamente, senza toccare `attempts`;
  - **ISSUE-032** (#404): backstop runtime nel resolver che rifiuta fuori da
    local una row enabled senza redirect (fail closed, non fallback all'env che
    farebbe entrare su un IdP diverso), più una migrazione dati che disabilita
    le row precedenti alla fix del write path;
  - **ISSUE-033** (#405): `allow()` restituisce un `BreakerLease` con il token
    del trial e gli outcome lo presentano; un outcome senza token resta traffico
    ordinario e non può risolvere un trial che non ha ottenuto. Una sola suite
    di conformità parametrizzata copre entrambi gli adapter, che avevano un file
    di test ciascuno — la ragione per cui la divergenza di #399 era passata.

## Deferred / product decision

- Il retry multi-canale degli alert resta volutamente at-least-once: se un
  canale riesce e l'altro fallisce, il retry ripete entrambi. È distinto da
  ISSUE-031 e dalla race closed da #396.
- Il ledger continua a usare float (R3-L15), scelta storica non alterata da
  queste remediation.

## Verified clean

- Pricing: validazione domain-level di tutti i rate finiti/non-negativi e
  CHECK DB sulle colonne scalari; create e update sono entrambi coperti.
- Cache: key v2 include modello, operazione, configurazione effettiva e request
  deny-list; semantic scope separa tenant, key, modello, operazione e contract.
- Client registry: entry ritirate tracciate separatamente e chiuse a shutdown.
- Deadline: budget residuo applicato a dispatch non-stream e stream open/prime.
- Purge: elimina state/outbox alert, grant ricevuti e routing decision con testo.

## Verified and refuted

- Nessuna regressione confermata nel pricing/cache/client registry: 115 test
  mirati passano e le riproduzioni del Round 13 non sono più ottenibili.
- Dedup e outbox sono ora una sola transazione; il claim CAS impedisce ai due
  dispatcher di inviare la stessa row nel normale caso concorrente.
- Il marker Redis sopravvive oltre il cooldown e chiude il precedente bug di
  TTL: ISSUE-033 riguarda soltanto l'associazione dell'outcome.

## Category scores

| Category | Score | Rationale |
|---|---:|---|
| Security & tenancy | 8.5/10 | SSO nuovo sicuro; migrazione legacy mancante |
| Correctness | 9.0/10 | Pricing/cache/purge corretti nei percorsi verificati |
| Async & concurrency | 7.5/10 | Outbox e breaker conservano tre transizioni incomplete |
| Persistence & transactions | 8.5/10 | Atomicità e migrazioni migliorate; legacy SSO resta |
| Billing / business invariants | 9.0/10 | Hard budget e cache accounting reggono |
| Architecture & maintainability | 8.5/10 | Buoni confini dominio/port; breaker richiede ownership esplicita |
| Testing | 9.0/10 | 92,94% e nuove regressioni; mancano gli interleaving stale/no-channel |
| Operations / production readiness | 8.0/10 | Alert e breaker hanno tail di recovery concreti |
| Frontend | 9.0/10 | Nessuna regressione UI emersa nel perimetro |

**Overall: 8.6/10** al momento della review. I tre MEDIUM sono stati chiusi
subito dopo (#403–#405); i punteggi qui sopra restano la fotografia di
`d4db533` e non sono stati riscritti a posteriori — la verifica delle
remediation spetta al round successivo.

Le tre correzioni condividono una lezione: **un protocollo concorrente ha bisogno
di un'identità, non solo di uno stato**. L'outbox sapeva che una riga era
claimed ma non da chi né perché rilasciarla; il breaker sapeva che un trial era
in volo ma non di chi fosse l'esito; la configurazione SSO sapeva di essere
enabled ma non se fosse ancora legale. In tutti e tre i casi la fix è stata
rendere esplicito il possessore.

# Code Review — Round 13 (2026-07-27)

[← Index](INDEX.md)

Tredicesima revisione, eseguita sull'intero tree corrente (`ccfc8e6`) con
attenzione particolare alla delta successiva al Round 12 (`498b468..ccfc8e6`):
response cache exact e semantica, failover e circuit breaker, budget alert,
pricing non-token, client registry, configurazione SSO su database, analytics,
retention/purge, CI, Docker e tooling di carico.

Tre lens indipendenti hanno lavorato in parallelo — sicurezza/RBAC/tenancy,
persistenza/concorrenza/invarianti economiche e API/integrations/UI/operations
— con una quarta passata coordinata guidata dal grafo del repository. Il
reviewer coordinatore ha riletto lo storico completo dei Round 1–12, verificato
ogni candidato sul tree primario e centralizzato deduplica, riproduzioni e
severità. Lo standard di inclusione resta rigoroso: soltanto difetti confermati
da una riproduzione oppure da una traccia deterministica end-to-end.

Baseline eseguita su `ccfc8e6`:

- `uv run pytest -q --cov=src/litestar_gateway --cov-report=term
  --cov-fail-under=80`: **1749 passed, 6 skipped**, copertura **92,89%**;
- `just test-postgres` su PostgreSQL 17 effimero, inclusa l'intera catena
  Alembic: **1755 passed**;
- `uv run ruff check`, `uv run ruff format --check` e
  `uv run pyrefly check`: verdi;
- `uv run pre-commit run --all-files --show-diff-on-failure`, incluso
  detect-secrets e rumdl: verde;
- `uv run pip-audit`: nessuna vulnerabilità Python nota; il pacchetto locale
  non pubblicato è l'unico skip;
- frontend: **43 test** e lint verdi; `pnpm audit --prod` non rileva
  vulnerabilità production. Il `node_modules` locale non conteneva
  `@playwright/test`, ma la build Docker ha eseguito install frozen e build
  Vite con successo, quindi il limite è dell'ambiente locale, non del lockfile;
- migrazioni su SQLite vuoto fino a `47e59bf43231`, `just migration-check`,
  `just ui-schema-check` e `just docs-check-links`: verdi;
- `just docker-ci`: immagine costruita e avviata, smoke `/health` **200 OK**.

## Executive summary

Il nucleo storico di autenticazione, isolamento tenant, cifratura dei secret,
SSRF pinning e unit of work continua a reggere. La copertura resta elevata,
PostgreSQL e Docker passano integralmente e non sono emerse vulnerabilità note
nelle dipendenze di produzione.

I nuovi rischi si concentrano in tre aree:

1. **integrità economica e del risultato**: le tariffe negative possono
   accreditare il ledger e aggirare un budget hard; le cache possono restituire
   una risposta di un modello, operazione o policy differenti;
2. **protocolli concorrenti incompleti**: l'outbox degli alert può sia perdere
   sia duplicare notifiche, il client registry perde una generazione di client
   e il breaker Redis non realizza il single-trial half-open;
3. **vincoli dichiarati ma non applicati end-to-end**: la deadline di failover
   non limita il tentativo in corso, la configurazione SSO su database riapre
   il fallback su `Host` già chiuso per la configurazione env e il purge si
   blocca sui nuovi record di alert.

Counts: **0 CRITICAL · 2 HIGH · 7 MEDIUM · 0 LOW**.

## Issue summary

| ID | Title | Severity | Files | Status |
|---|---|---|---|---|
| ISSUE-022 | Prezzi negativi accreditano il ledger e aumentano il budget disponibile | HIGH | `web/models/schemas.py`; `model_service.py`; `pricing.py` | Fixed by #392 |
| ISSUE-023 | Le cache possono riusare risposte tra modello, operazione e policy differenti | HIGH | `response_cache_key.py`; `response_cache_semantic.py`; `semantic.py`; `completion_service.py` | Fixed by #393 / #394 |
| ISSUE-024 | La cache semantica ha retention globale illimitata dei bucket | MEDIUM | `infrastructure/cache/semantic.py`; `web/teams/controller.py` | Fixed by #400 |
| ISSUE-025 | Il client registry perde il client sostitutivo durante close-on-release | MEDIUM | `infrastructure/llm/client_registry.py` | Fixed by #395 |
| ISSUE-026 | L'outbox dei budget alert può perdere o duplicare una notifica | MEDIUM | `usage_meter.py`; `budget_alert_state_repository.py`; `budget_alert_reconciler.py` | Fixed by #396 |
| ISSUE-027 | `overall_deadline_ms` non limita il tentativo di failover in corso | MEDIUM | `completion_service.py`; `cross-provider-failover.md` | Fixed by #397 |
| ISSUE-028 | SSO DB-backed riapre la redirect URI derivata da un `Host` non fidato | MEDIUM | `sso_settings_service.py`; `session/sso.py`; `SsoSettingsPage.tsx` | Fixed by #398 |
| ISSUE-029 | Il circuit breaker Redis salta il single-trial half-open | MEDIUM | `infrastructure/circuit_breaker.py`; `test_circuit_breaker.py` | Fixed by #399 |
| ISSUE-030 | Il purge del team è incompleto: alcune FK lo bloccano e le decisioni restano | MEDIUM | `team_repository.py`; `orm.py`; `test_retention_lifecycle.py` | Fixed by #401 |

## Findings

### ISSUE-022 — Prezzi negativi accreditano il ledger e aumentano il budget disponibile (HIGH)

**Dove.** `src/litestar_gateway/infrastructure/web/models/schemas.py:27-53`
e `:57-76` accettano ogni tariffa come `float`; il controller le inoltra nei
percorsi create e update in
`infrastructure/web/models/controller.py:74-108,159-191`. Il service non
valida né la costruzione (`application/model_service.py:74-143`) né le
modifiche (`:224-240`). Le colonne in
`infrastructure/persistence/orm.py:783-797` non hanno `CHECK >= 0` e
`domain/pricing.py:79-94` moltiplica direttamente le tariffe ricevute. Un
`MODEL_MANAGER` dispone di `MODELS_MANAGE`
(`domain/authorization.py:39-49`).

**Problema.** Create e update accettano tariffe negative per token ordinari,
cache write/read, immagini flat e ogni valore di `image_prices`. Il costo
calcolato diventa negativo e viene persistito nello stesso ledger usato dal
budget gate; non si tratta quindi soltanto di una rappresentazione UI errata,
ma di un credito economico effettivo.

**Perché è un problema.** Un model-manager è un ruolo team-scoped pensato per
amministrare i deployment, non per disabilitare i limiti di spesa. Configurando
un modello con prezzo negativo può aumentare il budget residuo a ogni chiamata
e consentire richieste che un hard cap avrebbe dovuto bloccare. Lo stesso dato
falsifica usage, analytics, savings e audit economico.

**Impatto verificato.** Una create HTTP con tariffe da `-1.0` a `-5.0` ha
risposto **201** restituendo i valori negativi. Con budget `$1`, tre risposte
mockate da 7 token prompt e 3 completion hanno tutte risposto **200**; il
ledger ha registrato `spent=-39.0` e il budget ha esposto
`remaining=40.0`. Una riproduzione pura ha inoltre confermato
`compute_cost(...)= -13.0`. Classificazione: **Confirmed, HIGH**.

**Correzione suggerita.** Introdurre una validazione condivisa nel dominio,
richiamata da create e update, che accetti soltanto numeri finiti `>= 0` per
ogni tariffa e per ogni valore di `image_prices`. Aggiungere vincoli DB dove il
dialetto lo consente e test HTTP per tutte le dimensioni, inclusi
`NaN`/`Infinity`, ruolo `MODEL_MANAGER`, zero esplicito e update parziale.

### ISSUE-023 — Le cache possono riusare risposte tra modello, operazione e policy differenti (HIGH)

**Dove.** La cache exact deriva la chiave dal nome modello e da una lista
parziale di campi in
`src/litestar_gateway/domain/response_cache_key.py:22-58`; il caller passa la
request pre-merge in
`application/completion_service.py:975-1007`. La vista semantica conserva
soltanto `messages`/`input` in
`domain/response_cache_semantic.py:29-62`. Il protocollo e l'adapter semantico
usano un namespace limitato a `(team_id, api_key_id)` in
`domain/ports/response_cache.py:63-91` e
`infrastructure/cache/semantic.py:27-87`; l'hit viene restituito e
contabilizzato come modello/operazione correnti in
`application/completion_service.py:338-355,464-516`.

**Problema.** La exact key include il nome canonico, ma non `model.id`, una
revisione/fingerprint della configurazione, l'operazione o l'intera request
effettiva dopo `params` e `params_enforced`; omette inoltre campi accettati che
cambiano il risultato, per esempio `n`, `parallel_tool_calls`, penalty e
opzioni reasoning. La cache semantica è ancora più ampia: non separa modello o
operazione e ignora `instructions`, tools, tool choice e formato di output.
Entrambi i percorsi chat e Responses partecipano
(`application/completion_service.py:975-987,1157-1180`).

**Perché è un problema.** Dopo un exact miss, una richiesta Responses per il
modello B può ricevere con HTTP 200 il body chat generato dal modello A per lo
stesso testo. Anche senza semantic tier, una variazione di policy, configurazione
o campo omesso dalla exact key può servire una risposta precedente. Tool call,
formato, numero di choice e istruzioni sono parte del contratto applicativo:
un hit errato è un risultato silenziosamente corrotto, non un degrado benigno.
Utenti applicativi distinti che condividono la stessa API key condividono anche
questo rischio.

**Impatto verificato.** Due request con stesso `input`, ma modelli,
`instructions` e tools diversi, producono la stessa vista semantica. Inserita
una risposta `model-a / A_ONLY` con vettore `[1, 0]`, la lookup della seconda
request nello stesso tenant l'ha restituita. Per la exact cache, aggiungere
`n=2` e `parallel_tool_calls=false` non ha modificato la chiave. Il percorso
`_dispatch` restituisce poi il body cached e lo attribuisce al modello
corrente. Isolamento tra team/API key e soglia di similarità sono presenti, ma
non proteggono l'input identico, con similarità `1.0`.

**Correzione suggerita.** Derivare una fingerprint della request effettiva e
della revisione di configurazione, includendo almeno `model.id`, operazione,
provider model, defaults/policy enforced e tutti i campi behavior-affecting.
Nel semantic tier, separare il namespace per modello/operazione/policy e
verificare i campi non semantici prima di accettare un nearest-neighbor hit.
Regressioni obbligatorie: cross-model, chat-versus-Responses, tools/policy,
`n`, update modello e delete/recreate con stesso nome.

### ISSUE-024 — La cache semantica ha retention globale illimitata dei bucket (MEDIUM)

**Dove.** `src/litestar_gateway/infrastructure/cache/semantic.py:23-44`
definisce un limite di 50 entry **per bucket**, ma `_buckets` non ha un tetto
globale. `find()` rimuove le scadenze soltanto dal bucket consultato
(`:46-72`); `add()` crea nuovi bucket senza sweep globale (`:74-88`). L'API di
emissione chiavi non impone una quota in
`infrastructure/web/teams/controller.py:357-391`.

**Problema.** Ogni coppia `(team_id, api_key_id)` crea un bucket permanente
finché quel medesimo bucket non viene nuovamente letto. Le chiavi revocate,
ruotate o non più usate lasciano quindi residenti body e vettori anche oltre
il TTL. Il limite locale evita soltanto che un singolo bucket cresca oltre 50,
non limita il numero di bucket.

**Perché è un problema.** Un amministratore del team può emettere un numero
non limitato di chiavi e popolare almeno un'entry per ciascuna. Il rate limit
IP di 120/minuto rallenta la crescita, ma non le assegna un limite finale. La
memoria del worker può quindi crescere fino a degradazione o OOM. Kill-switch
globale e opt-in per modello riducono l'esposizione e giustificano MEDIUM.

**Impatto verificato.** Con 1000 API key distinte l'adapter ha creato 1000
bucket. Dopo aver avanzato il clock oltre il TTL e aggiunto una nuova chiave,
il conteggio è diventato **1001**: i 1000 bucket scaduti e non rivisitati sono
rimasti residenti. Questo non duplica R6-M42, che riguardava la cache degli
embedding di routing, ora LRU-bounded globalmente.

**Correzione suggerita.** Aggiungere un limite LRU globale su bucket/entry,
sweep cross-bucket opportunistico e purge esplicito del namespace a
revoca/rotazione. Testare migliaia di tenant/key, expiry senza revisit e
concorrenza tra add/find.

### ISSUE-025 — Il client registry perde il client sostitutivo durante close-on-release (MEDIUM)

**Dove.** Un'entry `closing` è trattata come miss in
`src/litestar_gateway/infrastructure/llm/client_registry.py:168-176`; la nuova
generazione sovrascrive lo stesso slot in `:138-166`. Al rilascio della vecchia
lease, `_close_entry_locked()` esegue `pop(key)` senza verificare l'identità
dell'entry (`:186-190,231-240`). Lo shutdown chiude soltanto le entry ancora
tracciate (`:248-268`).

**Problema.** A capacità esaurita, una generazione leased viene marcata
close-on-release. Un nuovo lease della stessa chiave crea una seconda
generazione e sovrascrive il dizionario. Quando termina il lease della prima,
il `pop(key)` rimuove la seconda, pur chiudendo soltanto la prima. La nuova
generazione resta utilizzabile dal lease corrente ma scompare dal lifecycle
del registry.

**Perché è un problema.** I client provider possiedono pool HTTP, TLS context
e socket. Sotto churn concorrente, le generazioni perse non sono più riusabili
né chiudibili a shutdown, facendo crescere risorse native e connessioni oltre
la capacità dichiarata.

**Impatto verificato.** Con `capacity=1`: lease A1; lease B, che marca A1
closing; nuovo lease A crea A2; release A1. Prima del release A2 era lo slot
tracciato; dopo, A2 non era più in `_entries`. Anche dopo `aclose()`,
`a2.closed` è rimasto `False`, mentre A1 e B sono stati chiusi. La suite copre
eviction, rotazione e cancellation, ma non il reacquire della stessa chiave
durante close-on-release.

**Correzione suggerita.** Rimuovere lo slot soltanto quando
`self._entries.get(key) is entry` e mantenere esplicitamente le generazioni
ritirate finché l'ultimo lease non le chiude. Aggiungere la sequenza di
regressione sopra, verificando tracking, capacità e close esattamente una
volta.

### ISSUE-026 — L'outbox dei budget alert può perdere o duplicare una notifica (MEDIUM)

**Dove.** `src/litestar_gateway/application/usage_meter.py:761-812` esegue
`record_fired()` e poi `enqueue_alert()`. Il repository committa i due insert
separatamente in
`infrastructure/persistence/budget_alert_state_repository.py:46-87`. Il
dispatcher seleziona, invia esternamente e cancella senza claim in `:112-169`.
Ogni processo registra il worker tramite `app.py:451-471`, mentre
`infrastructure/budget_alert_reconciler.py:12-15` dichiara erroneamente sicura
la corsa fra repliche.

**Problema.** Il protocollo ha due finestre distinte:

1. un crash o errore dopo il commit del dedup ledger e prima del commit
   dell'outbox lascia una soglia "fired" senza alert pending; le valutazioni
   successive la saltano definitivamente;
2. due worker possono leggere la stessa riga, entrambi eseguire webhook/email
   e cancellarla soltanto dopo l'effetto esterno. Il delete idempotente non
   rende idempotente il send.

**Perché è un problema.** La prima finestra viola la durabilità dichiarata
dall'outbox e il commento in `usage_meter.py:765-769`, che afferma impossibile
lo stato fired-without-enqueue. La seconda moltiplica ogni alert per il numero
di repliche e può duplicare email o automazioni webhook. La duplicazione
documentata nel retry multi-channel è un trade-off distinto; qui anche una
consegna completamente riuscita viene ripetuta.

**Impatto verificato.** Un adapter di prova che fallisce su `enqueue_alert`
ha lasciato `durable_fired_count=1`, `outbox_count=0`; una seconda valutazione
non ha più tentato l'enqueue. In una riproduzione DB con due sessioni
indipendenti e barriera nel canale, entrambi i dispatcher hanno restituito
`1`, lo stesso alert ID è stato inviato **due volte** e la pending row è poi
scomparsa. Il test attuale è single-session.

**Correzione suggerita.** Inserire dedup row e outbox row nella stessa
transazione/repository operation. Per il drain, introdurre un claim/lease
atomico recuperabile: su PostgreSQL `FOR UPDATE SKIP LOCKED` più stato e
timeout, con strategia equivalente per SQLite; valutare anche una idempotency
key stabile per il destinatario. Aggiungere test fault-injection fra insert e
test concorrente multi-session/PostgreSQL.

### ISSUE-027 — `overall_deadline_ms` non limita il tentativo di failover in corso (MEDIUM)

**Dove.** Nei loop non-streaming e streaming, la deadline viene controllata
soltanto prima dei tentativi successivi in
`src/litestar_gateway/application/completion_service.py:1082-1103` e
`:1321-1342`. Le await che possono durare a lungo non sono avvolte dal budget
residuo (`:1119-1130` e `:1358-1386`). Il contratto in
`docs/next-steps/cross-provider-failover.md:90-97` la definisce invece come
wall-clock budget dell'intera chain.

**Problema.** Il primo tentativo, o qualunque retry avviato prima della
scadenza, può continuare fino al timeout SDK anche se supera ampiamente
`overall_deadline_ms`. Il test esistente usa una deadline illegale `-1` e
verifica soltanto che non inizi il secondo tentativo
(`tests/budgets/test_failover_orchestration.py:349-364`).

**Perché è un problema.** La proprietà serve precisamente a impedire
`slow-timeout × candidates` oltre la pazienza del caller. L'implementazione
attuale è un gate fra retry, non una deadline complessiva, e diverge anche fra
la configurazione amministrativa mostrata e il comportamento runtime.

**Impatto verificato.** Con deadline **10 ms** e primary che restituisce
successo dopo 120 ms, la chiamata non-streaming è terminata con successo in
**122,1 ms**. Una riproduzione indipendente sullo stream ha osservato lo stesso
ordine di grandezza durante open/prime. Nessun timeout o errore ha interrotto
il tentativo.

**Correzione suggerita.** Calcolare il budget monotonic residuo e applicare
`asyncio.timeout()`/`anyio.fail_after()` attorno a ogni dispatch e a
open+prime dello stream. Definire l'errore esposto alla scadenza e preservare
release, settlement e breaker bookkeeping. Testare successo lento, errore
lento, retry vicino al limite e cancellation per entrambi i percorsi.

### ISSUE-028 — SSO DB-backed riapre la redirect URI derivata da un `Host` non fidato (MEDIUM)

**Dove.** La configurazione env rifiuta SSO senza redirect fuori local in
`src/litestar_gateway/config.py:369-378`. Il percorso DB-backed valida
discovery URL, client id e secret, ma non `redirect_uri`, in
`application/sso_settings_service.py:37-75`. Il fallback usa
`request.base_url` in `infrastructure/web/session/sso.py:98-103`; non è
presente un allowed-host middleware. La console marca il campo come optional
e lo invia `null` in `ui/src/features/sso/SsoSettingsPage.tsx:70-83,198-208`.

**Problema.** Un platform admin può abilitare la nuova configurazione SSO
persistita senza callback fissa anche in staging/production. Quel percorso
aggira il controllo aggiunto per chiudere R4-M31 e torna a derivare la callback
dal `Host` della richiesta.

**Perché è un problema.** Una richiesta con Host forgiato può orientare la
redirect URI dichiarata nell'authorization request. Con IdP configurato con
matching wildcard/prefix questo amplia il rischio; con matching esatto causa
comunque steering/DoS del login. State, nonce e PKCE restano presenti, quindi
non si afferma un auth bypass generale e la severità resta MEDIUM.

**Impatto verificato.** `SsoSettingsService.upsert()` accetta
`enabled=true, redirect_uri=None` quando gli altri campi sono validi. Una
`Request` con `Host: attacker.example` produce
`https://attacker.example/sso/callback`. Il contratto documentale afferma
esplicitamente che una redirect fissa è richiesta fuori local
(`docs/enterprise-sso.md:61-66`).

**Correzione suggerita.** Iniettare la policy di deployment nel service e
rifiutare una configurazione DB enabled senza redirect in ambienti non-local;
rendere il campo required condizionalmente nella UI. Come difesa aggiuntiva,
configurare allowed hosts. Aggiungere test staging/production, request con Host
forgiato e hot reload della configurazione DB.

### ISSUE-029 — Il circuit breaker Redis salta il single-trial half-open (MEDIUM)

**Dove.** `src/litestar_gateway/infrastructure/circuit_breaker.py:128-139`
ritorna subito `True` quando la chiave `opened` non esiste. La stessa chiave
viene scritta con TTL esattamente uguale al cooldown in `:141-164`; il claim
`SET NX` della trial è raggiunto soltanto se il marker esiste ancora. Il
`FakeRedis` dei test registra i TTL ma non li applica in
`tests/routing/test_circuit_breaker.py:22-53`.

**Problema.** Nel Redis reale, al termine del cooldown il marker open scade.
`allow()` lo interpreta come breaker closed e non entra nel ramo half-open:
ogni richiesta concorrente viene ammessa, mentre il marker `trial` non viene
mai creato.

**Perché è un problema.** Dopo un outage, tutte le repliche possono colpire
simultaneamente il provider appena il TTL scade, causando uno stampede e
potenzialmente riaprendo l'incidente. L'adapter Redis diverge dal contratto e
dall'implementazione in-memory, che autorizza una singola trial.

**Impatto verificato.** Contro Redis 7 reale, con threshold 1 e cooldown 1 s:
durante il cooldown `allow=False`; dopo 1,2 s, cinque `allow()` concorrenti
hanno restituito `[True, True, True, True, True]` e
`trial_exists=False`. Il test corrente resta verde soltanto perché il fake non
fa scadere le chiavi.

**Correzione suggerita.** Conservare uno stato open oltre il cooldown oppure
modellare la transizione con uno script Lua/transaction atomico che converta
open in un unico claim half-open. Usare Redis reale o un fake TTL-faithful nei
test e coprire più processi concorrenti, trial success e trial failure.

### ISSUE-030 — Il purge del team è incompleto: alcune FK lo bloccano e le decisioni restano (MEDIUM)

**Dove.** `src/litestar_gateway/infrastructure/persistence/team_repository.py:159-202`
elimina soltanto i figli elencati a `:186-195`. Omette
`BudgetAlertStateModel`/`PendingBudgetAlertModel`, con FK verso `team.id` in
`infrastructure/persistence/orm.py:623-679`, e i grant ricevuti dal team,
`RouterGrantModel.team_id` (`orm.py:319-337`) e
`ModelGrantRecord.team_id` (`:827-852`). Omette inoltre
`RoutingDecisionModel` (`:352-399`), che non ha FK ma conserva `team_id`,
`user_text` e `system_prompt`. Il contratto di purge è in
`application/team_service.py:365-389` e
`infrastructure/web/teams/controller.py:248-274`; il test positivo inserisce
soltanto un usage event in `tests/teams/test_retention_lifecycle.py:195-230`.

**Problema.** Appena un team ha superato una soglia o ha ricevuto un grant,
una delle FK omesse impedisce la delete della team row. Una notifica pending
produce lo stesso risultato. L'`IntegrityError` viene tradotto in
`TeamNotEmpty`, quindi il purge risponde 409 e lascia intatti team e dati. Nei
casi senza figli bloccanti, il purge può riuscire ma lascia comunque le
routing decisions prive di FK, inclusi prompt potenzialmente sensibili.

**Perché è un problema.** Il nuovo endpoint amministrativo promette di
rimuovere irreversibilmente i dati di un team già tombstonato. Proprio i team
con una storia di budget alert — un caso ordinario — diventano non purgabili,
bloccando il workflow di retention e deletion. Audit e atomicità del purge
sono corretti, ma l'insieme dei figli è incompleto rispetto alle nuove tabelle.

**Impatto verificato.** Con FK SQLite abilitate, team soft-deleted e una riga
`BudgetAlertStateModel`, `SQLAlchemyTeamRepository.delete()` ha sollevato
`TeamNotEmpty`; `team_still_exists=True` e
`alert_state_still_exists=True`. Le stesse FK RESTRICT sono presenti su
pending alert e grant target. La traccia del percorso di successo conferma che
nessuna delete raggiunge `routing_decision`, che resta senza FK e fuori
dall'elenco; la catena PostgreSQL applica tutte le tabelle coinvolte.

**Correzione suggerita.** Eliminare esplicitamente pending/dedup state, grant
ricevuti e routing decisions nella stessa unit of work del purge, nell'ordine
corretto, documentando ciò che per policy deve invece sopravvivere. Estendere
il test endpoint con fired state, pending alert, grant modello/router e una
decisione contenente prompt: 204, tutte le righe team-scoped assenti e audit
del purge presente.

## Resolution status

- ISSUE-010–ISSUE-021 (Round 11 e Round 12): le remediation restano presenti
  sul tree corrente; non sono state rilevate regressioni nelle superfici
  precedentemente chiuse, salvo la **nuova superficie DB-backed** descritta in
  ISSUE-028 per la classe storica R4-M31.
- ISSUE-022–ISSUE-030: **tutti chiusi**. La review era report-only; la
  remediation è stata eseguita una PR per issue secondo
  [plans/16-round-13-remediation.md](../plans/16-round-13-remediation.md), ogni
  PR con una regressione che fallisce prima della fix:
  - **ISSUE-022** (#392): validazione dominio delle tariffe richiamata da create
    e update, CHECK sulle cinque colonne di prezzo, clamp dei valori negativi
    preesistenti nella migrazione;
  - **ISSUE-023** (#393 exact, #394 semantic): la chiave exact copre identità
    del modello, operazione e request effettiva post-merge con selezione a
    deny-list; il tier semantico vive dentro uno `SemanticScope` che la
    similarità non può attraversare;
  - **ISSUE-024** (#400): tetto LRU globale sugli scope e sweep opportunistico
    di quelli abbandonati;
  - **ISSUE-025** (#395): lo slot viene rimosso solo se contiene ancora
    quell'entry, e le generazioni ritirate restano chiudibili;
  - **ISSUE-026** (#396): dedup row e outbox row in una sola transazione, e
    claim atomico con lease prima di ogni invio;
  - **ISSUE-027** (#397): la deadline avvolge il dispatch e l'open+prime dello
    stream, non solo la decisione di ritentare;
  - **ISSUE-028** (#398): la configurazione SSO su database rifiuta
    `enabled` senza redirect fuori da local, come già faceva il percorso env;
  - **ISSUE-029** (#399): il marker open sopravvive al cooldown e il fake Redis
    dei test applica davvero i TTL;
  - **ISSUE-030** (#401): il purge elimina anche stato alert, grant ricevuti e
    routing decision.

## Deferred / product decision

- **Ledger monetario in `float`.** Resta il trade-off storico R3-L15: il nuovo
  pricing non-token amplia le dimensioni ma non cambia la scelta di
  rappresentazione. Passare a `Decimal`/minor units richiede una migrazione e
  una decisione di prodotto separata.
- **Credenziali globali e condivisione cross-team.** Resta il modello
  intenzionale documentato nei round precedenti; nessun nuovo bypass di
  autorizzazione è emerso.
- **Retry multi-channel degli alert.** Se webhook riesce ed email fallisce,
  il retry ripete entrambi i canali
  (`budget_alert_state_repository.py:128-134`). È una semantica at-least-once
  dichiarata e distinta dalla corsa multi-replica di ISSUE-026; valutare
  delivery state per canale se i duplicati applicativi non sono accettabili.
- **Preview dei router con strategie esterne.** Continua a ripiegare sul
  default model per evitare side effect nella preview, scelta esplicita già
  tracciata nel Round 12.

## Verified clean

- **Auth, RBAC e tenancy.** Exact e semantic cache includono team e API key,
  quindi non è stata rilevata una fuga cross-team. SSO settings e SCIM restano
  platform-admin-only; i client secret sono cifrati e non ritornano dalle API.
- **OIDC core.** Callback con state, nonce, PKCE, issuer/audience verification
  e algoritmi asimmetrici; ISSUE-028 riguarda esclusivamente il fallback della
  redirect URI nella nuova configurazione DB.
- **Webhook budget alert.** Riusa la protezione SSRF con DNS re-resolution e IP
  pinning; nessun bearer token di piattaforma viene inviato al destinatario
  del team.
- **Usage fallback.** `spend_since()` somma anche
  `pending_usage_event` non quarantinati
  (`usage_repository.py:219-245`), quindi una degradazione del ledger non apre
  una finestra di budget bypass e non è stata promossa a finding.
- **Streaming failover.** Open e primo chunk sono primati prima di esporre lo
  stream; release e settlement restano protetti sui percorsi verificati.
- **Provider client lifecycle ordinario.** Reuse, rotation, cancellation,
  eviction normale e shutdown sono coperti e funzionano; ISSUE-025 richiede la
  specifica interleaving same-key close-on-release.
- **Migrazioni e packaging.** Una sola head Alembic, upgrade e drift check su
  database vuoto, suite PostgreSQL 17, build Docker multi-stage e health smoke
  tutti verdi.
- **Supply chain di produzione.** Nessuna advisory Python nota e audit pnpm
  production pulito. Le tre advisory HIGH frontend osservate riguardano
  dipendenze dev/build e non entrano nel runtime bundle.

## Verified and refuted

- **Cache cross-team.** Confutata: entrambe le cache separano
  `(team_id, api_key_id)`. ISSUE-023 è cross-model/operation/policy all'interno
  dello stesso namespace autorizzato.
- **Semantic cache e data race asyncio.** I metodi in-memory non contengono
  await fra lettura e mutazione del bucket; non è stata dimostrata una race
  intra-loop. Il finding confermato è il limite globale assente.
- **Fallback usage e alert.** Confutata l'ipotesi che una usage row in outbox
  non partecipi alla soglia: `spend_since()` la include. La perdita confermata
  avviene invece fra i due commit dedup/outbox dell'alert.
- **Breaker sul primo tentativo.** Il fatto che il candidate scelto iniziale
  non venga filtrato dal breaker è una scelta esplicita del design; il finding
  riguarda soltanto la transizione Redis open→half-open.
- **Purge non autorizzato.** Confutato: il purge richiede platform admin,
  tombstone preliminare e audit nella stessa transazione. ISSUE-030 è un falso
  diniego causato dalle nuove FK, non un accesso eccessivo.
- **Build UI.** Il fallimento locale per `@playwright/test` assente non è un
  finding: package e lock lo dichiarano e una installazione frozen pulita
  dentro la build Docker compila correttamente l'intera UI.

## Category scores

| Category | Score | Rationale |
|---|---:|---|
| Security, auth & tenancy | 8.0/10 | Core solido; redirect SSO DB e cache policy richiedono correzione |
| Billing & business invariants | 6.5/10 | Prezzi negativi e protocollo alert compromettono due invarianti centrali |
| Async, concurrency & resilience | 6.5/10 | Registry, dispatcher, deadline e breaker hanno interleaving riprodotti |
| Persistence & migrations | 8.0/10 | Catena e UoW solide; purge incompleto sulle nuove FK |
| API, integrations & frontend | 8.5/10 | Contratti ampi e UI verde; validazione economica/SSO non uniforme |
| Tests, CI & production readiness | 8.0/10 | 92,89%, PostgreSQL e Docker verdi; fake/edge concorrenti troppo ottimistici |

**Overall: 7.4/10** al momento della review. I nove finding sono stati
successivamente remediati (#392–#401): la valutazione qui sopra resta la
fotografia del tree `ccfc8e6` e non è stata riscritta a posteriori — la
verifica delle remediation spetta al round successivo.

Due osservazioni della review si sono confermate durante la remediation e
valgono oltre i singoli finding: i fake troppo permissivi nascondevano difetti
reali (il `FakeRedis` senza TTL teneva verde un breaker rotto; il test
multi-replica del client registry costruiva un `Model` diverso per replica), e
la copertura multi-replica richiede sessioni indipendenti sullo stesso database
per riprodurre le corse davvero interessanti.

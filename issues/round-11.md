# Code Review — Round 11 (2026-07-22)

[← Index](INDEX.md)

Undicesima revisione completa, delimitata alla delta successiva al Round 10
(`d1e23d4..140c97b`) e riverificata contro il tree corrente. Il perimetro copre
le remediation del Round 10, modelli e router globali/estesi, Playground,
gestione credenziali, compatibilità dei parametri reasoning, nuove migrazioni,
console amministrativa e aggiornamenti di CI/deployment.

La mappa architetturale è stata ricostruita dall'indice Graphify esistente e
quattro passate indipendenti hanno coperto: sicurezza/RBAC/tenancy,
persistenza/billing, API/operations/UI e verifica coordinata cross-feature. I
finding sono deduplicati rispetto ai Round 1–10 e riportati solo dopo una
verifica sul codice corrente e, dove utile, una riproduzione HTTP/DB con
provider sintetici.

Baseline eseguita:

- `uv run pytest -q --cov=src/litestar_gateway --cov-fail-under=80`: **924
  passed, 6 skipped**, copertura **94,13%**;
- catena Alembic completa e suite su PostgreSQL effimero: **930 passed**;
- frontend: **24 test**, ESLint, TypeScript e build Vite verdi;
- pre-commit/Ruff, Pyrefly, `pip-audit`, MkDocs strict e `git diff --check`
  verdi;
- Alembic ha una sola head (`c6366c44d858`) e il controllo di drift dopo
  l'upgrade non rileva nuove operazioni.

## Executive summary

Counts: **0 CRITICAL · 4 HIGH · 6 MEDIUM · 0 LOW**.

Il nucleo di autenticazione, metering e streaming standard rimane solido, ma le
nuove superfici che effettuano o dirigono chiamate provider non conservano
ancora gli stessi invarianti. Il Playground è un percorso provider reale fuori
da budget, rate limit e ledger e accetta un fan-out duplicato non limitato. I
router condivisi restano mutabili dal tenant sorgente dopo l'estensione e
possono quindi essere trasformati in un webhook che riceve i prompt del tenant
destinatario. Inoltre alias e candidati sono risolti per nome in namespace
separati e nel contesto del caller: un router può essere ignorato a favore di un
modello omonimo, oppure eseguire il modello omonimo sbagliato.

La correzione SSRF storica R6-H18 va riaperta: il codice valida una risoluzione
DNS, ma lascia che il client HTTP risolva nuovamente lo stesso hostname senza
vincolare la connessione all'indirizzo approvato. Sul piano transazionale,
l'aggiornamento combinato di una credenziale può restituire 409 dopo aver già
sostituito il secret, e la promozione di un modello globale può eliminare i
grant prima di fallire. Reporting usage, scope delle API global-model,
accessibilità role-aware della UI e downgrade delle nuove migrazioni completano
i finding MEDIUM.

## Issue summary

| ID | Title | Severity | Files | Status |
|---|---|---|---|---|
| ISSUE-010 | Playground: chiamate provider reali fuori da budget, rate limit e ledger, con fan-out illimitato | HIGH | `web/playground/controller.py`; `application/playground_service.py`; `domain/authorization.py` | Open |
| ISSUE-011 | Un tenant sorgente può trasformare un router già condiviso in un sink dei prompt del tenant target | HIGH | `web/routing/controller.py`; `web/routing/platform_controller.py`; `persistence/router_repository.py`; `application/routing/{service,webhook}.py` | Open |
| ISSUE-012 | Alias e candidati dei router non hanno un'identità unificata: policy bypassate o modello sbagliato | HIGH | `application/{model_service,completion_service}.py`; `application/routing/service.py`; `web/api_router/models_list.py` | Open |
| ISSUE-013 | R6-H18 riaperto: l'IP validato dal guard SSRF non è quello vincolato alla connessione | HIGH | `application/routing/webhook.py` | Open |
| ISSUE-014 | PATCH credenziale non atomica: un 409 può comunque sostituire il secret senza audit | MEDIUM | `application/credential_service.py`; `persistence/credential_repository.py`; `web/credentials/controller.py` | Open |
| ISSUE-015 | L'alias `<base>-global` produce righe usage indistinguibili e un filtro che non trova i dati | MEDIUM | `persistence/usage_repository.py`; `web/teams/schemas.py`; `ui/src/features/usage/UsagePage.tsx` | Open |
| ISSUE-016 | Models, Routing e Playground della console non funzionano per i ruoli team dichiarati supportati | MEDIUM | `ui/src/features/{teams,models,routing,playground}`; `web/teams/controller.py`; `web/session/me.py` | Open |
| ISSUE-017 | PATCH/DELETE `/platform/models/{id}` accettano anche modelli team-owned e li auditano come globali | MEDIUM | `web/models/platform_controller.py`; `application/model_service.py`; `persistence/model_repository.py` | Open |
| ISSUE-018 | La promozione di un modello globale elimina i grant in transazioni separate | MEDIUM | `application/model_service.py`; `persistence/model_repository.py` | Open |
| ISSUE-019 | Il downgrade delle migrazioni global model/router fallisce in presenza di dati globali legittimi | MEDIUM | `migrations/versions/*90e784ecd46b.py`; `migrations/versions/*c6366c44d858.py` | Open |

## Findings

### ISSUE-010 — Playground fuori dal piano di governance (HIGH)

**Dove.** `infrastructure/web/playground/controller.py:46-51,81-110`,
`application/playground_service.py:59-71,114-149` e
`domain/authorization.py:38-45`.

**Problema.** `POST /playground/compare` richiede soltanto `MODELS_READ`, quindi
è disponibile anche al `model-manager`. `model_names` non ha limite né vincolo
di unicità; `compare()` crea una coroutine per ogni elemento e le esegue tutte
con `asyncio.gather()`. Ogni ramo decifra la credenziale e invoca direttamente
`LLMGateway.achat_completion()`, senza `UsageMeter.admit/settle`, budget,
limiter RPM di team/chiave o usage ledger. La descrizione dell'endpoint e la UI
confermano che si tratta di chiamate reali ma non contabilizzate.

**Perché è importante.** Un ruolo che non dispone necessariamente di una API
key inference può generare spesa reale non attribuita e aggirare tutte le policy
economiche. Duplicare lo stesso alias trasforma una singola richiesta HTTP in un
numero arbitrario di chiamate concorrenti, limitato solo dalla dimensione
generale del body.

**Impatto verificato.** Con team RPM=1, budget inferiore al costo di una chiamata
e un utente non-platform `model-manager`, una richiesta con tre copie dello
stesso alias ha restituito 201, generato tre chiamate provider e lasciato zero
righe usage. Un probe di servizio con 1.000 duplicati ha prodotto 1.000 invocazioni.

**Correzione suggerita.** Far passare ogni confronto dal normale percorso
`CompletionService`/`UsageMeter`, attribuendo attore e team; applicare budget,
RPM e audit; imporre un massimo di alias distinti, deduplicazione e un semaforo
di concorrenza. Se la non fatturazione è una scelta di prodotto, introdurre un
permesso dedicato ristretto e una quota separata, senza eliminare il ledger.

### ISSUE-011 — Router condiviso mutabile dopo l'approvazione (HIGH)

**Dove.** `infrastructure/web/routing/platform_controller.py:131-146`,
`infrastructure/web/routing/controller.py:283-297`,
`infrastructure/persistence/router_repository.py:154-169`,
`application/routing/service.py:383-427,533-595,639-690` e
`application/routing/webhook.py:91-110`.

**Problema.** Il platform admin estende un router mediante un grant verso il
record live del tenant sorgente. Dopo l'estensione, un `model-manager` del
tenant sorgente può ancora sostituire l'intera definizione con `PUT`, inclusi
strategia e shadow strategy, senza nuova approvazione e senza invalidare i
grant. Il caller target risolve proprio quel record aggiornato; una strategia
webhook invia `user_text` e `system_prompt` all'URL scelto dal sorgente.

**Perché è importante.** L'atto amministrativo di condividere una configurazione
apparentemente sicura concede di fatto al tenant sorgente un controllo futuro
sui prompt di altri tenant. La variante shadow è ancora meno visibile perché il
task è fire-and-forget e gli errori vengono assorbiti.

**Impatto verificato.** In un test E2E sintetico il platform admin ha esteso un
router Source a Target; successivamente un semplice Source model-manager lo ha
trasformato in webhook. Una inference con la chiave Target ha risposto 200 e il
transport webhook ha ricevuto sia il testo user sia il system prompt del target.

**Correzione suggerita.** Rendere i grant snapshot/versioni immutabili soggetti
a riapprovazione, oppure richiedere il platform admin per update/delete finché
esistono grant cross-team. Aggiungere consenso/revoca target e test di
regressione per webhook attivo e shadow configurati dopo l'estensione.

### ISSUE-012 — Risoluzione per nome senza namespace/identità unificata (HIGH)

**Dove.** `application/model_service.py:66-111`,
`application/routing/service.py:155-243,289-349`,
`application/completion_service.py:423-453` e
`infrastructure/web/api_router/models_list.py:36-49`.

**Problema.** Modelli e router verificano le collisioni in repository separati.
La completion risolve prima un modello e consulta il router solo se il modello
non esiste; `/v1/models` concatena i due cataloghi senza deduplicare. In più, i
candidati di un router esteso/globale restano semplici nomi e vengono risolti
nel contesto del team chiamante. `extend()` e `make_global()` non rivalidano
candidati e dipendenze per quel contesto.

**Perché è importante.** Un modello omonimo può oscurare silenziosamente un
router e saltarne strategia, cost/capability policy e decision logging. Un
router condiviso può invece risultare pubblicato ma non invocabile, oppure
selezionare il modello locale omonimo del target, con provider, parametri e
pricing diversi da quelli approvati.

**Impatto verificato.** (1) La creazione di un router globale `policy` e poi di
un modello team `policy` è stata accettata; `/v1/models` ha restituito due entry
`policy` e l'inference ha scelto sempre il modello. (2) Un router con candidato
privato del sorgente, esteso o promosso globale, è apparso nel catalogo target
ma ha restituito 404. (3) Quando il target possedeva un modello omonimo, la
stessa chiamata ha restituito 200 usando il `provider_model_id` e il pricing del
target, non quelli del sorgente.

**Correzione suggerita.** Introdurre un resolver/namespace unico per tutti gli
alias callable e validarlo in entrambe le direzioni, con vincoli race-safe.
Legare i candidati a identità stabili (model ID + provenienza) oppure materializzare
e validare esplicitamente la risoluzione per ogni target; la pubblicazione
globale deve validare una copia nel contesto globale. `/v1/models` deve esporre
alias univoci. Coprire collisioni model↔router, grant, global shadowing e
candidati omonimi cross-team.

### ISSUE-013 — R6-H18: DNS validation non vincolata al connect (HIGH)

**Dove.** `application/routing/webhook.py:103-110,122-137`.

**Problema.** `_ensure_public_target()` risolve l'hostname e rifiuta gli
indirizzi non pubblici, ma subito dopo viene creato un nuovo client e
`client.post(self._url, ...)` riceve ancora l'hostname. L'indirizzo validato
viene scartato: il transport effettua una seconda risoluzione DNS al momento
della connessione. Il commento e i report precedenti dichiaravano chiuso il DNS
rebinding, ma manca il binding tra check e use.

**Perché è importante.** Un hostname controllato può produrre una risposta
pubblica durante il guard e una destinazione privata durante la connessione,
riaprendo l'SSRF verso servizi interni. Il webhook riceve anche il contenuto del
prompt.

**Impatto verificato.** Instrumentando il percorso senza traffico esterno, il
resolver del guard ha restituito un IP pubblico mentre il transport ha osservato
come destinazione ancora l'hostname, confermando la seconda risoluzione non
vincolata. Restano efficaci i controlli su IP letterali, la verifica di tutti gli
indirizzi della prima risposta e il blocco dei redirect, ma non chiudono questo
TOCTOU.

**Correzione suggerita.** Connettersi esclusivamente a uno degli IP già validati,
preservando correttamente `Host` e TLS SNI/certificate validation, oppure
delegare il vincolo a un egress proxy/firewall che applica la stessa policy.
Aggiungere un test con due risoluzioni discordanti che dimostri che il secondo
indirizzo non viene mai usato.

### ISSUE-014 — PATCH credenziale con commit parziale (MEDIUM)

**Dove.** `application/credential_service.py:49-74`,
`infrastructure/persistence/credential_repository.py:103-124` e
`infrastructure/web/credentials/controller.py:149-169`.

**Problema.** Se una PATCH contiene sia `values` sia `name`, il servizio chiama
prima `replace_values()`, che committa, e controlla solo dopo la collisione del
nuovo nome. Il controller registra l'audit esclusivamente dopo il ritorno con
successo.

**Perché è importante.** La semantica osservabile non è atomica: un client vede
un fallimento e può ritentare o mantenere il vecchio stato mentale, mentre tutti
i modelli collegati stanno già usando il nuovo secret. Un errore di rename può
quindi causare outage senza evento `credential.update`.

**Impatto verificato.** PATCH di una credenziale con un nuovo secret sintetico e
il nome di un'altra credenziale ha restituito 409; rileggendo il DB, il nome era
rimasto invariato ma il secret era cambiato. Una inference successiva ha usato
il nuovo valore e non risultava alcun audit di update.

**Correzione suggerita.** Eseguire preflight del nome e aggiornamento di nome +
ciphertext in un singolo metodo repository/unit-of-work con un solo commit e
rollback comune; tradurre il vincolo unique dopo il rollback. Testare che ogni
409 preservi sia nome sia secret.

### ISSUE-015 — Attribution usage persa per gli alias globali (MEDIUM)

**Dove.** `infrastructure/persistence/usage_repository.py:48-89`,
`infrastructure/web/teams/schemas.py:48-68` e
`ui/src/features/usage/UsagePage.tsx:138-142`.

**Problema.** Quando un modello team `same` oscura il globale `same`, il globale
è callable come `same-global`. Il ledger persiste correttamente model ID e nome
canonico, ma non l'alias richiesto. L'aggregazione raggruppa per ID+nome, la
risposta elimina l'ID e la UI usa il nome come row key. Il filtro confronta
soltanto `model_name`, quindi `?model=same-global` non può trovare quelle righe.

**Perché è importante.** Due identità economiche diverse appaiono come due righe
indistinguibili con la stessa chiave React; filtri e attribuzione operativa sono
falsi proprio nel caso di shadowing introdotto dalla feature globale. Totali e
budget restano corretti, ma non è affidabile spiegare quale risorsa li abbia
generati.

**Impatto verificato.** Chiamate a `same` e `same-global` hanno prodotto due
aggregati API entrambi con `model: "same"`; il filtro `model=same-global` ha
restituito una lista vuota.

**Correzione suggerita.** Persistire l'alias effettivamente richiesto insieme
all'identità canonica e restituire almeno `model_id`, alias, origine/scope. Usare
l'ID come row key e definire chiaramente se il filtro opera su alias, nome
canonico o entrambi.

### ISSUE-016 — Console non realmente role-aware (MEDIUM)

**Dove.** `ui/src/features/teams/api.ts:26-40`,
`ui/src/features/models/ModelsPage.tsx:26-45`,
`ui/src/features/routing/RoutingPage.tsx:23-44`,
`ui/src/features/playground/PlaygroundPage.tsx:94-111`,
`infrastructure/web/teams/controller.py:64-83` e
`infrastructure/web/session/me.py:31-39`.

**Problema.** Models, Routing e Playground caricano sempre i team da
`GET /teams`, endpoint platform-admin, e le prime due pagine caricano anche le
collezioni `/platform/*`. Per gli utenti team-scoped esiste `/me/teams`, ma
queste superfici non lo usano. La sidebar le mostra comunque a ogni utente
autenticato.

**Perché è importante.** Team admin e model-manager hanno i permessi backend per
gestire/leggere le proprie risorse, ma la console fallisce prima di poter
selezionare il team. L'interfaccia dichiarata role-aware è quindi inutilizzabile
per ruoli legittimi e mostra errori 403 su percorsi che dovrebbero essere
supportati.

**Impatto verificato.** Per un model-manager membro di un team,
`GET /me/teams` e `GET /teams/{id}/models/callable` hanno restituito 200;
`GET /teams` e `GET /platform/models`, usati dalla UI, hanno restituito 403.

**Correzione suggerita.** Creare un unico hook `useAccessibleTeams`: `/teams`
per platform admin e `/me/teams` per gli altri; interrogare dati globali e
mostrare azioni platform solo quando autorizzati. Aggiungere test pagina per
platform admin, team admin, model-manager e ruoli read-only.

### ISSUE-017 — Endpoint global-model non global-scoped (MEDIUM)

**Dove.** `infrastructure/web/models/platform_controller.py:86-137`,
`application/model_service.py:159-184` e
`infrastructure/persistence/model_repository.py:144-169`.

**Problema.** PATCH e DELETE platform passano `team_id=None`; il servizio lo
interpreta come `get_any()` e il repository aggiorna/elimina per solo ID. Non
viene verificato che `model.team_id IS NULL`, pur descrivendo e auditando
l'operazione come globale.

**Perché è importante.** Non è un'escalation — serve un platform admin — ma
rompe il contratto e il confine operativo dell'API. Un UUID team-owned passato
per errore da automazione o operatore viene modificato o cancellato e l'audit lo
classifica falsamente come risorsa globale.

**Impatto verificato.** Un modello team è stato passato a
`PATCH /platform/models/{id}` e aggiornato con 200 mantenendo il team originale;
il successivo DELETE sullo stesso endpoint ha restituito 204 e rimosso il
modello dal tenant. Il percorso equivalente dei router filtra correttamente lo
scope globale.

**Correzione suggerita.** Introdurre `_get_global()` e repository operation con
predicato `team_id IS NULL`; restituire `ModelNotFound` per ID team-scoped.
Testare PATCH/DELETE con entrambi i tipi di ID e verificare il dettaglio audit.

### ISSUE-018 — Promozione modello non atomica (MEDIUM)

**Dove.** `application/model_service.py:186-199` e
`infrastructure/persistence/model_repository.py:144-165,201-203`.

**Problema.** `make_global()` controlla il nome, elimina ciascun grant chiamando
un metodo che committa, e solo alla fine aggiorna `team_id=None` con un altro
commit. Una creazione concorrente dello stesso nome globale dopo il precheck, o
un errore DB nell'update finale, lascia il modello team-owned ma privo dei grant
già rimossi.

**Perché è importante.** Una singola operazione amministrativa fallita può
revocare silenziosamente accessi esistenti e non è recuperabile con il rollback
dell'ultima transazione. Il repository router dispone già di una promozione
atomica equivalente, quindi la divergenza è evitabile.

**Impatto verificato.** Il percorso di commit è stato verificato direttamente:
ogni `remove_grant()` chiude una transazione prima dell'update finale; l'update
non traduce inoltre un eventuale `IntegrityError` di unicità. Il failure point è
raggiungibile da una race sul nome o da un errore DB finale.

**Correzione suggerita.** Implementare `promote_to_global()` nel repository come
un'unica transazione che aggiorna ownership e rimuove i grant, traducendo il
conflitto unique in `ModelNameExists`. Aggiungere una regressione a due sessioni
e una fault-injection che dimostri la conservazione dei grant dopo rollback.

### ISSUE-019 — Downgrade incompatibile con righe globali (MEDIUM)

**Dove.**
`migrations/versions/2026-07-22_nullable_model_team_id_model_grant_table_90e784ecd46b.py:136-160`
e
`migrations/versions/2026-07-22_nullable_router_team_id_router_grant__c6366c44d858.py:139-168`.

**Problema.** Entrambi i downgrade ripristinano `team_id nullable=False` e poi
eliminano le strutture global/grant, ma `data_downgrades()` è vuoto. Dopo l'uso
legittimo della feature esistono righe globali con `team_id NULL`; l'ALTER (o il
batch-copy SQLite) non può renderle NOT NULL. La migrazione router elimina anche
`origin_team_id`, che sarebbe l'unica provenienza disponibile per molte righe
promosse.

**Perché è importante.** Il runbook può dichiarare rollback supportato perché
la migration espone un downgrade, ma il primo ambiente che abbia creato un
modello/router globale non è più rollbackabile. Il fallimento avviene durante
un'operazione di recovery, quando la prevedibilità è essenziale.

**Impatto verificato.** La sequenza schema/data è incompatibile per vincolo: le
righe NULL restano presenti al momento di `alter_column(..., nullable=False)`;
non esiste alcuna policy di ripristino o rimozione prima dell'ALTER.

**Correzione suggerita.** Definire esplicitamente la policy dati prima del NOT
NULL: ripristinare il team originario dove disponibile e decidere come gestire
le risorse globali native, oppure dichiarare la migration irreversibile con un
errore intenzionale e documentato. Aggiungere un migration test con righe
globali reali per modello e router.

## Remediation plan — branch and PR stack

La remediation è divisa in otto PR di implementazione, precedute dalla PR che
registra il report e seguite da una PR documentale di chiusura. Una singola
mega-PR mescolerebbe rischi di rete, transazioni, migrazioni, contratti API e UI
e renderebbe sia la review sia il rollback inutilmente difficili.

```text
PR 0 — Report Round 11

Wave 1, sviluppabile in parallelo
├── PR 1 — SSRF pinning
├── PR 2 — Playground governance
├── PR 3 — Persistence integrity
└── PR 4 — Role-aware console

Wave 2, stack seriale
PR 5 — Callable alias registry
  └── PR 6 — Router revisions + candidate identity
       └── PR 7 — Usage attribution
            └── PR 8 — Migration rollback contract

PR 9 — Report remediation + release notes
```

Le PR 1–4 sono indipendenti e possono essere sviluppate in parallelo. Le PR
5–8 devono restare seriali: condividono resolver, routing service, identità
persistite e migrazioni; lo stack evita conflitti e più head Alembic.

### PR 0 — Registrare la review

- **Branch:** `docs/r11-code-review`
- **Titolo:** `docs: add Round 11 code review findings`
- **Scope:** questo report e `issues/INDEX.md`; nessuna modifica prodotto.
- **Exit criteria:** Markdown/pre-commit verdi e tutti gli ISSUE con ID stabile.
- **Rollback:** revert documentale senza impatto runtime.

### PR 1 — Pinning della destinazione webhook

- **Branch:** `fix/r11-013-webhook-ip-pinning`
- **Titolo:** `fix(security): pin validated webhook destinations`
- **Issue:** ISSUE-013.
- **Dimensione:** S/M.

Implementazione:

- separare risoluzione/validazione dalla connessione mediante un
  `ResolvedTarget` immutabile;
- connettersi esclusivamente a un IP approvato, conservando l'hostname originale
  per `Host`, TLS SNI e certificate validation;
- mantenere i redirect disabilitati e applicare la policy a IPv4, IPv6 e a ogni
  record DNS;
- documentare il comportamento in presenza di proxy, perché il proxy può
  diventare il vero enforcement point della risoluzione.

Test RED e acceptance criteria:

- il resolver restituisce un IP pubblico durante il guard e uno privato a una
  seconda chiamata: la seconda risoluzione non deve avvenire;
- TLS/SNI e verifica del certificato usano l'hostname originale;
- nessun fallback verso un IP non incluso nel set validato;
- i test esistenti su IP letterali, record misti e redirect restano verdi.

Il rischio principale è rompere TLS/SNI o proxy deployment. La PR deve restare
piccola e autonomamente revertibile.

### PR 2 — Governance del Playground

- **Branch:** `fix/r11-010-playground-governance`
- **Titolo:** `fix: enforce governance on playground provider calls`
- **Issue:** ISSUE-010.
- **Dimensione:** M.

Implementazione:

- eseguire ogni confronto attraverso `CompletionService` e il normale ciclo
  `UsageMeter.admit/settle`;
- applicare budget e rate limit di team/attore, con usage ledger e audit sempre
  presenti per le chiamate reali;
- introdurre un permesso esplicito `PLAYGROUND_EXECUTE` anziché riutilizzare
  `MODELS_READ`;
- imporre un massimo configurabile di alias distinti, deduplicazione e un
  semaforo di concorrenza;
- aggiornare la UI per dichiarare il consumo reale e mostrare errori di budget,
  quota e autorizzazione senza trasformarli in risultati vuoti.

Test RED e acceptance criteria:

- budget insufficiente e RPM=1;
- alias duplicati e superamento del limite di cardinalità;
- ruolo senza il nuovo permesso;
- ledger e audit valorizzati per ogni chiamata eseguita;
- concorrenza osservata mai superiore al limite configurato;
- fallimenti parziali restituiti senza perdere il settlement delle chiamate già
  concluse.

Questa PR deve entrare prima delle PR 5–6, che cambieranno il resolver usato
anche dal Playground. Il rollback disabilita la nuova esecuzione governata senza
toccare lo schema dati.

### PR 3 — Integrità transazionale e scope globale

- **Branch:** `fix/r11-persistence-integrity`
- **Titolo:** `fix: make credential and model lifecycle operations atomic`
- **Issue:** ISSUE-014, ISSUE-017 e ISSUE-018.
- **Dimensione:** M.

I tre finding condividono l'invariante “una use case applicativa, una
transazione”. Implementazione:

- PATCH credenziale con preflight del nome e aggiornamento di nome + ciphertext
  sotto un solo commit;
- traduzione degli `IntegrityError` soltanto dopo rollback completo;
- repository operation `get/update/delete_global()` vincolate da
  `team_id IS NULL`;
- promozione modello con cambio ownership e rimozione grant nella stessa
  transazione;
- audit emesso esclusivamente dopo il commit riuscito.

Commit interni consigliati:

```text
fix: make credential updates atomic
fix: enforce global model scope
fix: make model promotion atomic
```

Test RED e acceptance criteria:

- una collisione di rename preserva il vecchio secret e non crea audit;
- fault injection tra modifica e commit ripristina nome e ciphertext;
- race sul nome globale con due sessioni PostgreSQL;
- ogni fallimento di promozione preserva tutti i grant;
- PATCH/DELETE platform rifiutano un UUID team-owned e non producono un audit
  `global`;
- percorso positivo verificato sia su SQLite sia su PostgreSQL.

Va mergiata prima della PR 5 perché entrambe toccano `model_service.py` e
`model_repository.py`. Il rollback è applicativo e non richiede downgrade.

### PR 4 — Console role-aware

- **Branch:** `fix/r11-016-role-aware-console`
- **Titolo:** `fix(ui): load resources according to the caller capabilities`
- **Issue:** ISSUE-016.
- **Dimensione:** M.

Implementazione:

- introdurre un hook condiviso `useAccessibleTeams`;
- usare `/teams` per platform admin e `/me/teams` per utenti team-scoped;
- caricare collezioni `/platform/*` e mostrare azioni globali soltanto in
  presenza delle relative capability;
- preferire capability restituite dal backend alla duplicazione della matrice
  ruoli dentro React;
- mantenere distinguibili loading, forbidden, failure ed empty state.

Test RED e acceptance criteria:

- pagine Models, Routing e Playground per platform admin, team admin,
  model-manager e ruolo read-only;
- nessuna richiesta `/platform/*` da un utente privo di capability;
- selezione dei soli team accessibili mediante `/me/teams`;
- azioni mutate coerenti con i permessi, non soltanto nascoste visivamente;
- test UI, ESLint, TypeScript e build Vite verdi.

La PR è indipendente. È preferibile mergiarla prima del cambio di contratto del
catalogo nelle PR 5–6; il rollback riguarda esclusivamente il frontend salvo
l'eventuale aggiunta di capability alla risposta `/me`.

### PR 5 — Registro unico degli alias callable

- **Branch:** `refactor/r11-012-callable-alias-registry`
- **Titolo:** `refactor: unify callable alias resolution`
- **Issue:** prima parte di ISSUE-012.
- **Dimensione:** L.

Questa è la fondazione architetturale dello stack seriale. Implementazione:

- un solo registro/resolver per alias di modelli e router;
- namespace unico per scope team, grant e globale, con vincoli race-safe;
- eliminazione della precedenza implicita “model prima, router dopo”;
- resolver condiviso da completion, Playground e `/v1/models`;
- catalogo con alias univoci, tipo esplicito (`model`/`router`) e identità
  stabile;
- preflight delle collisioni già presenti, senza rinomina automatica e
  silenziosa;
- mapping coerente delle collisioni a un errore di dominio 409.

Matrice di test RED:

- creazione model→router e router→model;
- collisioni local→global e global→local;
- grant verso un team con alias occupato;
- creazioni concorrenti in sessioni separate;
- `/v1/models` senza duplicati;
- risoluzione identica sugli endpoint OpenAI, Anthropic e Gemini compatibili;
- nessuna regressione di shadowing intenzionale mediante suffisso `-global`.

La migrazione deve fallire in preflight con un elenco operativo delle collisioni
invece di scegliere automaticamente un vincitore. Il rollback richiede che il
vecchio resolver resti eliminabile senza perdere le identità già persistite.

### PR 6 — Revisioni router e candidati con identità stabile

- **Branch:** `fix/r11-router-revisions`
- **Base iniziale:** `refactor/r11-012-callable-alias-registry`.
- **Titolo:** `fix: version shared routers and bind candidates to model identities`
- **Issue:** ISSUE-011 e seconda parte di ISSUE-012.
- **Dimensione:** L.

Implementazione:

- salvare i candidati mediante model ID stabile e provenienza, non soltanto
  tramite nome;
- introdurre revisioni router immutabili;
- fare puntare ogni grant alla revisione esplicitamente approvata;
- trasformare l'update del tenant sorgente nella creazione di una nuova
  revisione, senza modificare i grant esistenti;
- rendere l'upgrade del grant esplicito, auditato e riservato al platform admin;
- consentire a un router globale di referenziare soltanto modelli globali;
- richiedere che le dipendenze di un router esteso siano esplicitamente
  accessibili al target;
- impedire la cancellazione di revisioni ancora referenziate o definirne una
  revoca atomica e auditata.

Test RED e acceptance criteria:

1. il platform admin estende una revisione Source a Target;
2. il Source model-manager configura un webhook attivo o shadow in una nuova
   revisione;
3. Target continua a usare la revisione approvata;
4. nessun prompt Target raggiunge il webhook;
5. soltanto dopo approvazione esplicita la nuova revisione diventa attiva;
6. un modello omonimo nel target non cambia l'identità del candidato;
7. candidati eliminati/disabilitati e grant revocati producono un errore di
   dominio deterministico.

La migrazione deve effettuare backfill dei candidati esistenti nel contesto del
router proprietario e fermarsi sulle risoluzioni ambigue. Il rollback deve
preservare la revisione attiva e non cancellare automaticamente la cronologia.

### PR 7 — Attribution usage completa

- **Branch:** `fix/r11-015-usage-attribution`
- **Base iniziale:** `fix/r11-router-revisions`.
- **Titolo:** `fix: preserve requested and resolved callable identity in usage`
- **Issue:** ISSUE-015.
- **Dimensione:** M.

Implementazione:

- aggiungere al ledger `requested_alias`, `resolved_model_id`, nome canonico e
  origine/scope;
- propagare la stessa identità attraverso outbox, settlement, streaming ed
  endpoint nativi;
- restituire ID, alias e nome canonico nelle API usage;
- definire e documentare se ogni filtro opera su alias, nome canonico o entrambi;
- usare un ID stabile come row key nella UI;
- eseguire un backfill conservativo, marcando come sconosciuto ciò che non è
  ricostruibile invece di inventare alias storici.

Test RED e acceptance criteria:

- modello locale e globale omonimi chiamati come `same` e `same-global`;
- filtri per alias e identità;
- righe UI distinte e stabili;
- totale budget invariato;
- streaming, endpoint OpenAI/Anthropic/Gemini e riconciliazione outbox;
- dati storici senza alias ancora leggibili e chiaramente marcati.

Dipende dalla PR 5 per l'identità callable e dalla PR 6 per l'identità stabile
dei router. Il rollback deve mantenere compatibili in lettura le righe prive dei
nuovi campi.

### PR 8 — Contratto di downgrade delle risorse globali

- **Branch:** `fix/r11-019-migration-rollback-contract`
- **Base iniziale:** `fix/r11-015-usage-attribution`.
- **Titolo:** `fix(migrations): make global resource downgrades data-safe`
- **Issue:** ISSUE-019.
- **Dimensione:** M.

Implementazione:

- riassegnare le risorse promosse al relativo `origin_team_id` prima di
  ripristinare il NOT NULL;
- per risorse globali native prive di origine, interrompere il downgrade prima
  di qualsiasi DDL con un errore chiaro e azionabile;
- non inventare un team e non cancellare risorse automaticamente;
- aggiungere un comando/runbook di preflight e una procedura esplicita di
  riassegnazione o rimozione;
- eseguire questa PR per ultima, affinché il test copra anche tutte le nuove
  migrazioni dello stack.

Test RED e acceptance criteria:

- upgrade→seed globale→downgrade→upgrade su SQLite e PostgreSQL;
- modello e router promossi con `origin_team_id`;
- risorsa globale nativa senza origine, con abort prima del DDL;
- grant e revisioni esistenti;
- una sola Alembic head e nessun drift finale;
- documentazione del rollback verificata eseguendo i comandi del runbook.

### PR 9 — Chiusura Round 11

- **Branch:** `docs/r11-remediated`
- **Titolo:** `docs: mark Round 11 findings as remediated`
- **Scope:** stato e riferimenti delle PR per ISSUE-010–ISSUE-019,
  `issues/INDEX.md`, release notes e migration notes.
- **Exit criteria:** tutti gli ISSUE chiusi da test di regressione, baseline
  completa riportata e nessuna dichiarazione “fixed” basata soltanto sul diff.
- **Rollback:** documentale; deve seguire, non precedere, l'ultimo merge prodotto.

### Merge order and release checkpoints

Ordine di merge:

1. `docs/r11-code-review`;
2. `fix/r11-013-webhook-ip-pinning`;
3. `fix/r11-010-playground-governance`;
4. `fix/r11-persistence-integrity`;
5. `fix/r11-016-role-aware-console`;
6. `refactor/r11-012-callable-alias-registry`;
7. `fix/r11-router-revisions`;
8. `fix/r11-015-usage-attribution`;
9. `fix/r11-019-migration-rollback-contract`;
10. `docs/r11-remediated`.

Checkpoint consigliati:

- dopo PR 4: release **v1.4.1**, hardening senza il cambio architetturale degli
  alias/router;
- dopo PR 8: release **v1.5.0**, perché alias registry, candidate identity,
  revisioni router e schema usage costituiscono un cambiamento architetturale e
  potenzialmente contrattuale.

Ogni PR di implementazione deve rispettare lo stesso gate:

1. test RED nel primo commit;
2. implementazione GREEN minima;
3. refactor senza mutare il comportamento coperto;
4. test mirati, suite completa SQLite e PostgreSQL, coverage ≥80%, Pyrefly e
   pre-commit;
5. verifica del diff, note di compatibilità e rollback;
6. review sicurezza obbligatoria per PR 1, 2, 5 e 6;
7. commit convenzionali e descrizione PR con mapping agli ISSUE chiusi.

## Resolution status — OPEN

La revisione è stata eseguita in sola lettura sul codice prodotto. Nessuno dei
finding ISSUE-010–ISSUE-019 è stato corretto in questo round e non sono stati
creati commit. La sola modifica preesistente nel worktree è `.DS_Store`.

Ordine raccomandato:

1. chiudere immediatamente ISSUE-010 e ISSUE-013, perché espongono chiamate di
   rete/spesa fuori dagli invarianti dichiarati;
2. congelare o riapprovare i router condivisi (ISSUE-011) e introdurre identità
   stabili/namespace unificato (ISSUE-012);
3. rendere atomici credential update e model promotion (ISSUE-014/018);
4. correggere attribution, UI, scope API e strategia downgrade
   (ISSUE-015/016/017/019).

## Deferred / product decision

- L'uso dei `float` nel ledger resta il trade-off esplicitamente accettato in
  Round 3 (L15); nessuna nuova regressione nella delta.
- La condivisione cross-team di credenziali è documentata come scelta di
  prodotto in `docs/security-hardening.md`; non è stata riclassificata come
  finding.
- Se i router estesi devono essere riferimenti live e non snapshot, serve
  comunque una decisione esplicita su consenso, notifica e riapprovazione delle
  modifiche sensibili: l'attuale comportamento non rende visibile quel rischio.

## Verified clean

- **Auth/RBAC:** scadenza e rotazione API key, separazione principal umano/API
  key, filtri team delle routing decisions, OIDC expired-token e guard SCIM/SSO
  restano corretti nei percorsi verificati.
- **Inference standard:** admission/settlement, budget totals, key/team RPM,
  cleanup streaming/cancellation, chiusura client e mapping errori provider
  mantengono le remediation dei round precedenti.
- **Adapter reasoning:** il retry OpenAI `max_tokens` →
  `max_completion_tokens` è immutabile, ristretto al relativo bad request e
  coperto sia stream sia non-stream.
- **Persistenza:** una sola head Alembic; upgrade completo e drift check verdi su
  SQLite, upgrade + suite completa verdi su PostgreSQL; promozione router
  atomica a livello DB.
- **Supply chain / CI:** `pip-audit` non rileva vulnerabilità note nelle
  dipendenze risolvibili; pre-commit, type-check, docs strict e build UI sono
  verdi.
- **Qualità test:** 924 test backend con 94,13% di coverage, 930 su PostgreSQL e
  24 test UI passano.

## Verified and refuted

- Il nuovo retry reasoning non duplica i payload o il metering: la prima
  risposta è un bad request provider e il settlement avviene solo sulla risposta
  valida.
- Il totale economico usage non perde le chiamate `-global`: ISSUE-015 riguarda
  identità, filtro e presentazione, non la somma usata dal budget.
- La promozione router è atomica nel repository; il suo difetto è semantico
  (candidati risolti tardi e non rivalidati), non un partial commit analogo a
  ISSUE-018.
- Gli endpoint team-scoped backend autorizzano correttamente team admin e
  model-manager; ISSUE-016 è una regressione di orchestrazione UI/API, non un
  diniego nel service layer.
- IP privati letterali, risposte DNS iniziali miste e redirect webhook sono
  effettivamente bloccati; ISSUE-013 riguarda esclusivamente la seconda
  risoluzione non pinata.

## Category scores

| Category | Score | Summary |
|---|---:|---|
| Security / tenancy | **5.5/10** | Auth core solido, ma router condivisi e guard SSRF aprono due confini cross-tenant/rete ad alto impatto. |
| Money / rate limiting | **6/10** | Inference standard corretta; il Playground aggira l'intero piano di governance e l'attribution degli alias è ambigua. |
| Routing / model identity | **5.5/10** | Feature ricca ma alias separati e late binding dei candidati possono saltare policy o scegliere il modello sbagliato. |
| Persistence / migrations | **6.5/10** | Upgrade e PostgreSQL verdi; restano due UoW parziali e un downgrade senza policy dati. |
| Admin UI / API | **6.5/10** | Build pulita e pagine complete, ma i ruoli team non possono usare tre superfici dichiarate accessibili e lo scope global-model è lasco. |
| Test / CI | **9/10** | Coverage 94,13%, suite PostgreSQL e gate completi; mancano test sugli invarianti cross-feature introdotti dalla delta. |

**Overall: 6.4/10.** La qualità meccanica e la copertura sono alte, ma le nuove
funzioni globali/condivise hanno introdotto percorsi che non ereditano ancora le
garanzie economiche, di identità e di tenancy del gateway principale.

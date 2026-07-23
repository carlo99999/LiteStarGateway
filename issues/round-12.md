# Code Review — Round 12 (2026-07-22)

[← Index](INDEX.md)

Dodicesima revisione, delimitata alla delta successiva al Round 11
(`140c97b..498b468`): le nove PR di remediation #313–#321 (pinning webhook,
governance del Playground, integrità transazionale credenziali/modelli,
console role-aware, registro unico degli alias callable, revisioni router
immutabili, attribution usage, contratto di downgrade delle risorse globali)
più i commit documentali di chiusura. Ogni verifica è stata eseguita contro il
tree corrente (`498b468`).

Sei lens indipendenti hanno lavorato in parallelo — sicurezza/RBAC/tenancy,
correttezza/async/concorrenza, persistenza/migrazioni, invarianti economici,
API/UI e una passata adversarial cross-feature — con verifica finale e
assegnazione di severità centralizzate nel reviewer coordinatore. Lo standard
di inclusione è invariato: solo finding confermati sul codice corrente o
inferenze fortemente supportate; i candidati teorici sono registrati ma
esclusi dai Findings. ISSUE-020 è stato riprodotto con uno script dedicato su
SQLite effimero.

Baseline eseguita su `498b468`:

- `uv run pytest -q --cov=src/litestar_gateway --cov-fail-under=80`: **1000
  passed, 6 skipped**, copertura **92,79%** (3 min 37 s);
- `just test-postgres` (PostgreSQL 17 effimero, catena Alembic completa via
  `database upgrade`, poi suite intera): **1006 passed** (3 min 21 s);
- `uv run ruff check` e `uv run ruff format --check`: verdi (349 file);
- `uv run pyrefly check`: verde;
- `uv run pre-commit run --all-files` (incluso detect-secrets e rumdl) e
  `git diff --check`: verdi;
- `uv run --with pip-audit pip-audit`: nessuna vulnerabilità nota (il solo
  skip è il pacchetto locale `litestar-gateway`, non pubblicato su PyPI);
- frontend: **31 test** e build Vite verdi;
- `uv run alembic heads`: una sola head, `c83e4a1b7d52`.

## Executive summary

La remediation del Round 11 regge alla verifica adversarial. I quattro HIGH
storici sono chiusi in modo verificabile sul codice corrente: il pinning
webhook connette esclusivamente a un IP già validato preservando Host/SNI
(tracciato fino a `httpcore` 0.28.1 installato), i grant router puntano a
revisioni immutabili con upgrade esplicito platform-admin e CAS ottimistico,
il registro unico `callable_alias` elimina lo shadowing per nome con vincoli
unici a livello DB validi anche per lo scope globale su entrambi i dialetti, e
il Playground passa per il normale ciclo admission/settlement con permesso
dedicato, dedup e concorrenza limitata. Le dieci combinazioni cross-feature
sondate (omonimi team/global, tombstone/reclaim degli alias, edit post-grant,
rotazione credenziali in-flight, fan-out Playground, cancellazioni con usage
storico, riapprovazione webhook, identità candidati, promozioni) risultano
tutte SAFE con meccanismo verificato.

Il tema dei nuovi difetti è la **coda non uniforme delle remediation**: le
protezioni introdotte per una superficie non sono state propagate alla
superficie gemella. Il ciclo di vita dei router condivisi ha il guard
`RouterShared`, ma la delete di un modello team-owned con grant attivi li
cascade-elimina in silenzio (ISSUE-020); la console è diventata role-aware per
Models/Routing/Playground (PR #315), ma Usage e Budgets restano irraggiungibili
proprio per `billing-viewer` e per l'auditor di piattaforma, i due ruoli che il
backend autorizza a leggerle (ISSUE-021).

Counts: **0 CRITICAL · 0 HIGH · 2 MEDIUM · 0 LOW**.

## Issue summary

| ID | Title | Severity | Files | Status |
|---|---|---|---|---|
| ISSUE-020 | La delete di un modello con grant attivi li cascade-elimina in silenzio, senza il guard `RouterShared`-equivalente | MEDIUM | `application/model_service.py`; `persistence/model_repository.py`; `persistence/orm.py` | Remediated (#332) |
| ISSUE-021 | Usage e Budgets della console restano inaccessibili a `billing-viewer` e auditor, ruoli autorizzati dal backend | MEDIUM | `ui/src/features/teams/access.ts`; `ui/src/features/usage/UsagePage.tsx`; `ui/src/features/budgets/BudgetsPage.tsx` | Remediated (#333) |

## Findings

### ISSUE-020 — La delete di un modello con grant attivi li revoca in silenzio (MEDIUM)

**Dove.** `src/litestar_gateway/infrastructure/persistence/model_repository.py:241-247`
(`remove()`), `src/litestar_gateway/application/model_service.py:228-234`
(`delete()`), `src/litestar_gateway/infrastructure/persistence/orm.py:654`
(`model_grant.model_id` con `ondelete="CASCADE"`) e
`src/litestar_gateway/infrastructure/persistence/orm.py:732`
(`callable_alias.model_grant_id`, anch'esso CASCADE). Endpoint:
`DELETE /teams/{team_id}/models/{model_id}`
(`infrastructure/web/models/controller.py:189-203`).

**Problema.** `RouterRepository.delete()` rifiuta la cancellazione di un
router con grant attivi sollevando `RouterShared`
(`router_repository.py:392-401`), invariante introdotto proprio in questa
delta. Il percorso gemello dei modelli non ha alcun guard: `remove()`
tombstona l'alias e cancella la riga `model`; le FK `ondelete="CASCADE"`
eliminano i `model_grant` e i relativi `callable_alias` dei team target. Non
esiste una `ModelShared` in `domain/exceptions.py` e nessun audit registra la
revoca dei grant.

**Perché è un problema.** I grant modello vengono creati esclusivamente dal
platform admin (`web/models/platform_controller.py:176`), ma la delete del
modello sorgente richiede soltanto `MODELS_MANAGE` sul team proprietario: un
model-manager del team sorgente può quindi annullare, senza errore e senza
traccia, un atto amministrativo di condivisione verso altri team, che perdono
l'alias richiamabile a runtime. È esattamente la classe di rischio che il
Round 11 ha chiuso per i router; la divergenza fra le due superfici gemelle è
essa stessa introdotta da questa delta (i `model_grant` nascono con
`90e784ecd46b`).

**Impatto verificato.** Riprodotto con uno script su SQLite in-memory (FK
attive): creati team sorgente/target, modello `shared-model` e un grant verso
il target, `SQLAlchemyModelRepository.remove(model_id)` è terminata senza
errore e la rilettura ha mostrato `grants: 1 → 0`. Nessun test della suite
copre la delete di un modello con grant attivi
(`tests/models/test_platform_models.py` cancella solo il grant, mai il
modello sorgente). Classificazione: **Confirmed** (riprodotto).

**Correzione suggerita.** Specchiare il contratto router: in
`ModelRepository.remove()` (e nel percorso `remove_global()` per simmetria)
verificare l'esistenza di `model_grant` sotto `lock_resource_lifecycle` e
sollevare una nuova `ModelShared` mappata a 409, richiedendo la revoca
esplicita dei grant prima della delete. In alternativa, se il cascade è la
scelta di prodotto, documentarlo, emettere un audit per ogni grant revocato e
coprirlo con un test. Regressione: delete di un modello con grant → 409;
delete dopo la revoca → 204.

### ISSUE-021 — Usage e Budgets inaccessibili ai ruoli billing autorizzati dal backend (MEDIUM)

**Dove.** `ui/src/features/teams/access.ts:89-100`
(`canAccessConsoleSurface`), `ui/src/app/layout/Sidebar.tsx:20`,
`ui/src/features/usage/UsagePage.tsx:87` e
`ui/src/features/budgets/BudgetsPage.tsx:42` (entrambe con `listAllTeams`).
Contratto backend: `src/litestar_gateway/domain/authorization.py:51`
(`BILLING_VIEWER` → `USAGE_READ`, `BUDGET_READ`) e `:58-60`
(`AUDITOR_TEAM_PERMISSIONS`, identiche capability su ogni team);
`application/team_service.py:224-231` (`GET /teams` riservato al platform
admin).

**Problema.** `canAccessConsoleSurface` gestisce esplicitamente `dashboard`,
`audit` e le superfici model-family, poi ricade su `return false` per tutto il
resto: `usage` e `budgets` non compaiono mai nella sidebar per un utente non
platform-admin, incluso chi ha il ruolo `billing-viewer` o il flag auditor. Il
helper corretto esiste (`canReadUsage`, `access.ts:77-79`) ma il gate di
navigazione non lo usa. Anche navigando per URL diretto, entrambe le pagine
caricano il selettore team con `listAllTeams()` (`GET /teams`,
platform-admin-only) invece di `useAccessibleTeams()`/`GET /me/teams`: la
richiesta risponde 403, il selettore mostra l'errore, `teamId` resta vuoto e
le query usage/budget (`enabled: teamId.length > 0`) non partono mai.

**Perché è un problema.** È la stessa classe di difetto di ISSUE-016 (falso
diniego: la console non consente ciò che il backend autorizza), sulle due
superfici che la PR #315 non aveva in scope. Il backend definisce
`billing-viewer` esattamente per `USAGE_READ`/`BUDGET_READ` e concede
all'auditor le stesse letture su ogni team, ma per entrambi i ruoli le pagine
sono permanentemente vuote. Il test esistente cristallizza il gap senza
accorgersene: `access.test.ts:70` verifica `usage=false` per un model-manager,
ma nessun caso copre `billing-viewer` o l'auditor, e nessun test copre
`budgets`.

**Impatto verificato.** Tracciato end-to-end su entrambi i lati:
`Sidebar.tsx:20` filtra via `canAccessConsoleSurface` → `false`
(`access.ts:99`); `UsagePage.tsx:87`/`BudgetsPage.tsx:42` →
`listAllTeams` → 403 dal service layer (`team_service.py:224-231`), mentre
`GET /teams/{id}/usage` e `GET /teams/{id}/budget` autorizzerebbero il ruolo
(`web/teams/controller.py`, guard `USAGE_READ`/`BUDGET_READ`).
Classificazione: **Confirmed** (traccia deterministica su codice UI e
backend; nessuna condizione esterna richiesta).

**Correzione suggerita.** Estendere `canAccessConsoleSurface` con il ramo
billing (`usage`/`budgets` → `canReadUsage` sui ruoli, più `isAuditor`) e
passare `UsagePage`/`BudgetsPage` a `useAccessibleTeams()` come già fatto per
Models/Routing/Playground nella PR #315. Regressione: test pagina e test di
`access.ts` per `billing-viewer` e auditor su entrambe le superfici; nessuna
chiamata `GET /teams` da utenti non platform-admin.

## Resolution status

- ISSUE-010–ISSUE-019 (Round 11): restano **fixed on main**; questa review ne
  ha riverificato le correzioni sul tree corrente senza trovare regressioni o
  fix incompleti sulle superfici rimediate.
- ISSUE-020, ISSUE-021: individuati in questo round (review-only) e **rimediati
  subito dopo su main** — ISSUE-020 con #332 (guard sulla delete di modelli con
  grant attivi), ISSUE-021 con #333 (Usage/Budgets role-aware per
  `billing-viewer`/auditor). Nessun finding resta aperto.

## Deferred / product decision

- **Delete team e ledger usage.** `TeamService.delete_team()`
  (`application/team_service.py:260-276`) elimina esplicitamente anche
  `usage_event`/`pending_usage_event` del team
  (`persistence/team_repository.py:144-146`). È un comportamento pre-esistente
  alla delta, documentato nel docstring e platform-admin-only, quindi non è un
  finding di questo round; è però in tensione con l'obiettivo di durabilità
  dell'attribution introdotto da ISSUE-015. Decisione di prodotto suggerita:
  retention/anonimizzazione (`team_id` anonimizzato o export obbligatorio)
  invece della cancellazione fisica.
- **Preview router nel Playground.** `select_preview()`
  (`application/routing/service.py:660-703`) non esegue mai strategie esterne
  (judge/embeddings/webhook) e ripiega su `default_model`: per quei router la
  preview può indicare un modello diverso da quello che una chiamata reale
  sceglierebbe. Trade-off deliberato (zero side effect non governati in
  preview), coperto da `test_router_preview_never_runs_an_external_strategy`;
  va solo confermato come scelta di prodotto.
- Restano deferred i temi storici: `float` nel ledger (R3-L15) e condivisione
  cross-team delle credenziali (documentata come scelta di prodotto).

## Verified clean

- **Pinning webhook (ISSUE-013).** `_ensure_public_target()` restituisce gli
  IP validati e `_post_to_approved_address()` connette a
  `url.copy_with(host=<ip>)` con `extensions={"sni_hostname": host}`;
  tracciato dentro `httpcore` 0.28.1 installato: `server_hostname` guida SNI e
  verifica del certificato. Regressioni in
  `tests/routing/test_webhook_shadow.py:186-246` (singola risoluzione DNS, IP
  pinnato on-wire, Host/SNI corretti).
- **Revisioni router e grant pinnati (ISSUE-011/012).** Ogni `update` crea una
  revisione immutabile con CAS (`RouterRevisionConflict`) senza toccare i
  grant; `get_for_grant()` carica sempre la revisione pinnata;
  `upgrade_grant()` è platform-admin-only con ri-validazione candidati e nuova
  ack di egress; i candidati si risolvono per `model_id` stabile
  (`resolve_model_id`), mai per nome, quindi un omonimo nel team target non
  può sostituirsi al candidato approvato; `make_global()` ri-valida l'intera
  configurazione nello scope globale prima della promozione. Il task shadow
  ri-verifica grant e revisione live prima di eseguire
  (`routing/service.py:1068-1103`) e degrada a "nessuna entry", mai a un leak.
- **Registro alias callable (ISSUE-012).** TOCTOU chiuso a livello DB:
  insert + `claim_direct()` nella stessa transazione con traduzione degli
  `IntegrityError`; unicità globale garantita da indici parziali
  dialect-matched (`uq_global_callable_alias`, `uq_global_model_name`,
  `uq_global_router_name` con `WHERE team_id IS NULL` su SQLite e Postgres);
  tombstone/reclaim del ciclo delete/recreate corretto senza name-lock né
  hijack cross-team; `/v1/models` costruito da un unico snapshot senza
  duplicati.
- **Governance Playground (ISSUE-010).** `PLAYGROUND_EXECUTE` dedicato, max 5
  alias dedupati, semaforo di concorrenza, ogni ramo attraverso
  `CompletionService.chat_completion()` (admission, RPM, settlement, ledger,
  trace); `BudgetExceeded`/`RateLimited` propagano come 402/429 senza
  righe-risultato silenziose.
- **Integrità transazionale (ISSUE-014/017/018).** PATCH credenziale con
  preflight nome e un solo commit; `update_global`/`remove_global` vincolate a
  `team_id IS NULL`; promozione modello e router in un'unica transazione con
  traduzione dei conflitti unique dopo rollback.
- **Attribution usage (ISSUE-015).** `requested_alias`, `resolved_model_id`,
  nome canonico, origine e `source_team_id` persistiti su ledger e outbox; il
  filtro `model` combina alias richiesto, nome canonico e nome; `row_id`
  stabile in UI; `spend_since` somma per team indipendentemente da
  `api_key_id` NULL (righe da sessione console mai perse dai totali budget);
  reconcile outbox idempotente con event time originale preservato.
- **Migrazioni e rollback (ISSUE-019).** I downgrade global-resource eseguono
  il preflight (native-global, origini mancanti, collisioni di nome) prima di
  ogni DDL e falliscono lasciando il DB alla revisione corrente; il registro
  alias e le revisioni router documentano il write freeze richiesto e
  bloccano il downgrade quando la storia non è rappresentabile; `justfile`
  espone `migration-global-downgrade-preflight` read-only; 7 scenari coperti
  in `tests/migrations/test_global_resource_downgrade.py` e catena verificata
  su PostgreSQL 17.
- **Console role-aware (ISSUE-016, superfici rimediate).** Models, Routing e
  Playground usano `useAccessibleTeams()` e gate coerenti con
  `domain/authorization.py`; ack di egress imposti anche server-side; i nuovi
  dialog rendono gli errori delle mutation invece di stati vuoti;
  `ui/openapi.json` e `schema.ts` allineati ai nuovi endpoint e ai campi
  `UsageResponse`.

## Verified and refuted

- **`except A, B:` senza parentesi** (`application/playground_service.py:51,208`,
  `application/routing/webhook.py:143`): verificato che con il floor
  `requires-python = ">=3.14"` (PEP 758) la forma è legale e identica alla
  tupla parentesizzata; la suite la esercita e si comporta correttamente. Non
  è un difetto; nota di hardening: parentesizzare, perché l'aggiunta futura di
  `as exc` diventa `SyntaxError` e nessuna regola Ruff configurata la segnala.
- **Race sull'attribution delle decision usage**
  (`routing/service.py:172,1133-1142`: `last_decision_record_id` è stato
  mutabile d'istanza): teorico e non raggiungibile — `RouterService` è
  istanziato per request e il Playground serializza con
  `DEFAULT_MAX_CONCURRENCY = 1` non sovrascrivibile dal controller. Da
  tenere presente se la concorrenza del Playground venisse mai alzata;
  interesserebbe solo le righe di osservabilità/savings, mai il ledger budget.
- **`slot_reserved()` non filtra i tombstone**
  (`persistence/callable_alias_repository.py:110-120`): la disambiguazione
  degli alias di extend tratta un alias liberato come ancora occupato e
  sceglie un suffisso; over-conservativo, fail-safe, nessun blocco né hijack.
- **Fallback legacy in `RouterService.list_callable`**
  (`routing/service.py:537-550`, ramo `get_any` senza revisione pinnata):
  morto nel wiring di produzione — la DI inietta sempre il resolver
  (`web/routing/dependencies.py:36-49`, `web/api_router/dependencies.py:95-103`);
  usato solo da test double non cablati e solo per display.
- **Retry reasoning e nuova identità usage**: il retry
  `max_tokens → max_completion_tokens` resta immutabile e con settlement
  unico sulla risposta valida; nessuna interazione con revisioni o alias.

## Category scores

| Category | Score | Summary |
|---|---:|---|
| Security & tenancy | **9.5/10** | Zero nuovi finding di sicurezza; pinning SSRF, revisioni immutabili, permesso Playground dedicato e scope globale verificati end-to-end, incluse le librerie installate. |
| Correctness | **9.0/10** | Nessun difetto runtime nella delta; ISSUE-020 è una violazione di contratto del ciclo di vita, non un errore di calcolo. |
| Async & concurrency | **9.5/10** | Lock di lifecycle, CAS sulle revisioni, vincoli unici race-safe e re-check dei task shadow chiudono le schedule sondate; l'unica race candidata è teorica e non raggiungibile. |
| Persistence & transactions | **9.0/10** | UoW atomiche e downgrade data-safe verificati su entrambi i dialetti; il cascade non guardato dei `model_grant` (ISSUE-020) è l'unico neo. |
| Billing / business invariants | **9.5/10** | Admission/settlement, budget, RPM, ledger e attribution richiesta/risolta verificati su tutti i percorsi della delta, Playground incluso. |
| Architecture & maintainability | **8.5/10** | Registro alias e revisioni ben stratificati; restano l'asimmetria guard model/router e gli `except` non parentesizzati come debito minore. |
| Testing | **9.5/10** | 1000 test SQLite (92,79%), 1006 PostgreSQL, 31 UI e regressioni dedicate per ogni fix R11; mancano i casi delete-con-grant e billing-viewer individuati qui. |
| Operations / production readiness | **9.0/10** | Runbook di freeze/preflight documentati e provati; gate CI completi; una sola head Alembic. |
| Frontend | **8.0/10** | Superfici rimediate solide e error rendering corretto; ISSUE-021 ripropone il falso-diniego su Usage/Budgets per i ruoli billing. |

**Overall: 9.1/10.** La remediation del Round 11 è confermata robusta sotto
verifica adversarial: nessun HIGH o CRITICAL nuovo, invarianti economici e di
tenancy tenuti anche nelle combinazioni cross-feature. I due MEDIUM aperti
sono code non uniformi delle stesse remediation — il guard di condivisione non
propagato ai modelli e la console billing non allineata al RBAC backend — ed
entrambi hanno correzioni piccole e ben delimitate.

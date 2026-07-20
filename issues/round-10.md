# Code Review — Round 10 (2026-07-21)

[← Index](INDEX.md)

Decimo pass completo, eseguito sui cambi successivi al Round 9 (`d4c2871..d1e23d4`,
PR #248–#280) e ricontrollato contro l'albero corrente. Il delta copre: le remediation
del Round 9 (#249–#259), le pagine console per service principals / credentials /
models / routing / observability, la dashboard role-aware con i nuovi endpoint
(`GET /me/teams`, `GET /teams/{id}/savings`, `GET /routing/savings`,
`service_principal_id` in `KeyResponse`), il refresh docs e il quiet-404 logging.

Quattro pass indipendenti (security, correttezza Python, TypeScript/UI, adversarial
cross-feature); ogni finding riportato qui è stato riverificato sul codice reale con
citazioni `file:line`. La suite e i gate statici sono verdi (872 passed; Ruff, Pyrefly,
pre-commit, ESLint, tsc e build Vite puliti).

## Executive summary

**Nessun finding CRITICAL, e — per la prima volta — nessun finding di sicurezza HIGH.**
Le remediation del Round 9 reggono a un pass adversarial dedicato: mask/envelope del
bearer token webhook corretti su tutto il round-trip PUT, lifecycle invito↔team
mutuamente esclusivo via lock, rotate che ripristina owner/SP e blocca le key non
attive, per-key RPM su tutte le superfici di inferenza, `/me/teams` self-scoped,
savings gated correttamente.

Il finding più importante è di **modellazione dati**: le `routing_decision` sono
chiavate per `(team_id, router_name)` senza `router_id`. Un router cancellato continua
per sempre a gonfiare savings/stats di team e piattaforma, e — peggio — **riusare il
nome** di un router cancellato contamina il nuovo router con la storia del vecchio,
incluso l'export JSONL di distillazione che trasporta prompt utente grezzi tra due
configurazioni logicamente distinte.

Sul piano UI il tema ricorrente è la **fabbricazione di empty/zero-state su errore**:
la dashboard mostra "$0.00" di spesa se la fetch delle organizzazioni fallisce, la
pagina Budgets risponde "spend illimitata" a un errore di fetch, stats/savings/audit
rendono "—"/vuoto indistinguibile da un 403 o un 500.

Counts: **0 CRITICAL · 2 HIGH · 4 MEDIUM · 3 LOW**.

## Issue summary

| ID | Titolo | Priorità | File coinvolti | Stato |
|---|---|---|---|---|
| ISSUE-001 | Le routing decision sono chiavate per nome: router cancellati inquinano i savings e il riuso del nome contamina la storia (export incluso) | high | `persistence/orm.py:212-223`; `domain/ports/routing.py:41-59`; `application/routing/service.py:586-640`; `persistence/router_repository.py:279-288` | **Fixed** (#282) |
| ISSUE-002 | La dashboard mostra "$0.00 · no spend recorded" quando la fetch delle organizzazioni fallisce | high | `ui/src/features/dashboard/DashboardPage.tsx:117-152` | Open |
| ISSUE-003 | `GET /me/teams` tronca silenziosamente a 100 membership e fa N+1 lookup | medium | `application/team_service.py:284-293`; `web/session/me.py:31-39` | Open |
| ISSUE-004 | Le pagine Budgets / Audit / Router-detail rendono gli errori come empty-state ("illimitato", "no events", "—") | medium | `ui/src/features/budgets/BudgetsPage.tsx:117-140`; `ui/src/features/dashboard/DashboardPage.tsx:105-108,218-221`; `ui/src/features/routing/RouterDetailPage.tsx:135-142,183-198` | Open |
| ISSUE-005 | Lo `0` esplicito nei campi costo/threshold viene scartato senza feedback (`parsePositive`) | medium | `ui/src/features/models/CreateModelDialog.tsx:35-40,252-259`; `ui/src/features/routing/CreateRouterDialog.tsx:67-72,558-568` | Open |
| ISSUE-006 | Race `delete_user`↔`add_member`: la FK violation viene rietichettata `AlreadyMember` (409 fuorviante) | medium | `application/user_service.py:211-224`; `persistence/membership_repository.py:21-39` | Open |
| ISSUE-007 | `_savings_aggregate` esegue 3 SELECT non-atomiche per una cifra sola | low | `persistence/router_repository.py:290-323` | **Fixed** (#282) |
| ISSUE-008 | La guard di `delete_user` è una lista manuale di FK: una futura FK verso `user_account` produrrebbe un 500 | low | `application/user_service.py:201-225` | Open |
| ISSUE-009 | Cast diffusi `error as Error \| null` sugli errori di useQuery | low | ~15 call site in `ui/src/features/*` | Open |

## Findings

### ISSUE-001 — Routing decision chiavate per nome (high)

`RoutingDecisionModel` ha solo `team_id` + `router_name` — nessuna colonna
`router_id`, nessuna FK verso `router` (`persistence/orm.py:212-223`). La delete del
router è hard e non tocca le decision (`persistence/router_repository.py:171-178`);
il vincolo `UniqueConstraint("team_id", "name")` (`orm.py:169-171`) vale solo tra i
router *esistenti*, quindi il nome torna libero subito. Tutta la lettura —
`list_decisions`, `distribution`, `savings`, l'export JSONL — filtra per
`(team_id, router_name)` (`domain/ports/routing.py:41-59`,
`application/routing/service.py:586-640`); `team_savings`/`platform_savings`
aggregano ogni riga senza join verso `router`
(`persistence/router_repository.py:279-288`).

Riproduzione: (a) crea "prod-router", genera traffico, cancellalo → i suoi numeri
restano per sempre dentro `GET /routing/savings` e `GET /teams/{id}/savings`, non
esclusi ed etichettati in dashboard solo come "all time"; (b) ricrea un router con lo
stesso nome e strategia/candidati diversi → `decisions`, `stats`, `savings` e
`/decisions/export` del *nuovo* router mostrano mescolata la storia del vecchio.
L'export di distillazione trasporta prompt utente grezzi: la misattribuzione tra due
configurazioni distinte è anche un problema di igiene dati, non solo di metriche.

Fix suggerito: aggiungere `router_id: UUID` (nullable, senza FK cascade — la storia
deve sopravvivere alla delete di proposito) a `routing_decision`, popolarla in
scrittura e filtrare gli endpoint per-router per id. Se i totali team/piattaforma
devono includere i router cancellati è una scelta di prodotto legittima, ma va detta
(copy UI/docs); gli endpoint scoped su *un* router non devono mai mostrare la storia
di un altro solo perché condivideva il nome.

### ISSUE-002 — Dashboard: "$0.00" su errore della fetch org (high)

`DashboardPage.tsx:117-152`: se la query `["organizations","all"]` fallisce
(`retry:false`), `spendQueries` diventa un array vuoto; il guard di rendering
`spendLoaded || spendQueries.length === 0` è vero e mostra `formatUsd(0)` =
**"$0.00"**, con sotto "no spend recorded yet.". Nella stessa schermata la card
"organizations" mostra correttamente "—" per lo stesso errore: la pagina si
contraddice, e una vista finanziaria riporta un falso zero invece di uno stato di
errore. Fix: branch esplicito su `orgs.isError` (e `orgs.isLoading`) per il pannello
spend.

### ISSUE-003 — `/me/teams` troncato a 100 + N+1 (medium)

`TeamService.list_user_teams` (`team_service.py:284-293`) chiama
`memberships.list_by_user` senza `limit` → cade sul default `DEFAULT_PAGE_SIZE`
(100): un utente in 101+ team non vede le membership oltre la centesima, senza alcun
segnale di troncamento (l'endpoint non ha parametri di paginazione). In più fa un
`teams.get()` per membership (N+1, bounded a 100). Fix: batch fetch (`WHERE id IN`),
più paginazione esplicita oppure iterazione fino a esaurimento, dato che il
contratto è "tutti i miei team".

### ISSUE-004 — Errori resi come empty-state in Budgets/Audit/Router detail (medium)

Stesso pattern su tre superfici: `BudgetsPage.tsx:117-140` non consulta mai
`budget.isError` e su un errore di fetch mostra il copy "no budget configured — this
team's spend is unlimited" (il layer API distingue correttamente 404 da errore dopo
la fix di R9 ISSUE-013, ma la pagina non consuma il segnale);
`DashboardPage.tsx:218-221` rende un errore della query audit come "no audit events
yet."; `RouterDetailPage.tsx:135-142,183-198` rende stats/savings come "—" identico
per loading, 403 e 500 (mentre la tabella decisions sotto passa correttamente
`error` alla DataTable). Fix: branch `isError` con messaggio, come già fanno le
pagine-lista.

### ISSUE-005 — `0` esplicito scartato dai form (medium)

`parsePositive` richiede `n > 0`: nei costi del model
(`CreateModelDialog.tsx:252-259`, input con `min="0"`) uno `0` legittimo ("questo
modello è gratis") diventa `null` = "non impostato" senza feedback; nella threshold
delle route embeddings (`CreateRouterDialog.tsx:558-568`, `min="0" max="1"`) il
backend richiede `0 < t <= 1` e defaulta a `0.80`: chi digita `0` riceve in silenzio
una threshold 0.80. Fix: validazione inline (errore visibile per i valori fuori
contratto) e `min` allineati al range reale; accettare `0` dove il backend lo
consente.

### ISSUE-006 — Race delete_user ↔ add_member rietichettata (medium)

`delete_user` prende `FOR UPDATE` sull'utente, verifica assenza di membership/key e
cancella (`user_service.py:211-224`). Un `add_member` concorrente (pre-check +
INSERT, `membership_repository.py:21-39`) che perde la race fallisce con
`IntegrityError` per FK sul parent mancante — ma l'adapter cattura *qualsiasi*
`IntegrityError` e alza incondizionatamente `AlreadyMember`: l'admin riceve un 409
"già membro" per un utente appena cancellato. Fix: distinguere la causa (FK
violation vs unique) o ricontrollare l'esistenza dell'utente prima di rietichettare;
merita un test mirato, in linea con le race chiuse in R9.

### ISSUE-007 — Aggregato savings non point-in-time (low)

`_savings_aggregate` (`router_repository.py:290-323`) esegue tre SELECT separate
(SUM, counted, all) senza snapshot: sotto traffico live `decisions_without_usage`
può risultare incoerente (in un caso avverso negativo, non è clampato). Solo
reporting, non billing. Fix: collassare in una query
(`COUNT(*) FILTER (WHERE …)` + SUM condizionale) — corregge e riduce 3 round trip a 1.

### ISSUE-008 — Guard di delete_user come lista manuale (low)

La guard controlla solo membership e `api_key.created_by`; oggi non esistono altre
FK verso `user_account` non gestite (verificato: `password_reset` è pulita nel
delete, `audit.actor_id` non è FK), quindi **non è un bug attivo** — ma una futura
FK aggiunta senza aggiornare la guard produrrebbe un 500 generico (rollback
corretto, nessuna corruzione). Trappola manutentiva: commentare la guard con
l'invariante o derivare il check dalle FK a runtime nei test.

### ISSUE-009 — Cast `as Error | null` (low)

~15 call site castano `useQuery().error` a `Error | null`. Oggi ogni `queryFn`
costruisce `new Error(...)` reali, quindi è un no-op sicuro; ma una futura rejection
non-Error verrebbe mistipata in silenzio. Fix a basso costo: narrowing
`instanceof Error` in un helper condiviso.

## Ipotesi verificate e confutate (per il prossimo round)

- **Round-trip enable/disable del router webhook**: il mask `***` echato sul PUT è
  ripristinato dall'envelope cifrato in `_preserve_masked_tokens`
  (`router_repository.py:73-99,149-169`), anche per la sezione `shadow`; i candidate
  dict di risposta combaciano 1:1 con `CandidateRequest`. Nessuna perdita/corruzione.
- **Invito → team cancellato → redeem**: `register()` e `delete_team()` si
  serializzano su `lock_for_lifecycle`; `register` verifica l'esistenza del team
  *prima* di bruciare l'invito. La fix #253 regge.
- **Rotate + `service_principal_id`**: il rotate ricontrolla owner/SP sotto lock e
  propaga `service_principal_id`; una key non attiva non è ruotabile.
- **Cambio finestra budget a metà periodo**: `window_start` è ricalcolato stateless
  a ogni lettura — nessun contatore persistito da invalidare.
- **Trasporto invite token post-#254**: solo URL fragment, catturato in store
  in-memory e ripulito con `history.replaceState` prima che il router osservi la
  location; il check del path è coerente col basepath `/ui`.
- **Locale italiano nei campi numerici**: `<input type="number">` normalizza sempre
  il separatore — la virgola non è un vettore.
- **Nota (non issue)**: `provide_principal` ora autentica anche via cookie di
  sessione; il percorso è protetto (SameSite=Strict + CSRF sulle mutazioni), ma il
  trust boundary di `Principal` include ora un umano via cookie — da tenere presente
  aggiungendo nuovi endpoint gated su `provide_principal`.

## Category scores (this round)

| Categoria | Score | Sintesi |
|---|---:|---|
| Security / RBAC | **8.5/10** | Zero finding: le remediation R9 reggono a un pass adversarial dedicato; authz dei nuovi endpoint corretta end-to-end. |
| Money / rate limiting | **8/10** | Copertura RPM completa; i savings sono solo reporting ma il keying per nome ne mina l'attendibilità (ISSUE-001). |
| Persistence / lifecycle | **7.5/10** | UoW e lock ben usati; restano il keying per nome delle decision e una race di etichettatura (ISSUE-006). |
| Admin UI | **7/10** | Typed, paginata, token hygiene a posto; il tema aperto è la resa degli errori come empty/zero-state (ISSUE-002/004/005). |
| Test / CI | **8.5/10** | 872 verdi e gate forti; mancano test sugli stati di errore UI e sul riuso del nome router. |

**Overall: 7.8/10.** Salto netto dal 6.8 del Round 9: il core sicurezza/tenancy non ha
prodotto finding nuovi e le fix precedenti sono confermate da verifica avversaria. Il
lavoro rimasto è robustezza di prodotto: un `router_id` sulle decision e stati di
errore onesti nella console.

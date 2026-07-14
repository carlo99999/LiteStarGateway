# Code Review — Round 9 (2026-07-14)

[← Index](INDEX.md)

Nono pass completo, eseguito sui cambi successivi al Round 8 (`d2f86a4..d4c2871`) e
ricontrollato contro l'albero corrente. Il focus principale è la nuova admin UI e le
feature di organizzazioni/team, inviti, rate limit per team/API key e rotazione delle
API key. I finding dei Round 1–8 non vengono ripetuti.

La suite e i gate statici sono verdi (`807 passed`; Ruff, Pyrefly, pre-commit,
pip-audit, ESLint e build Vite puliti), ma i test happy-path non coprono alcune
interazioni tra RBAC, rotazione, disattivazione utenti e nuove foreign key. I finding
più importanti sono stati riprodotti end-to-end con `AsyncTestClient` contro SQLite
con foreign key abilitate, cioè con la stessa semantica di integrità attesa in
Postgres.

## Executive summary

Il problema più urgente è una privilege escalation verificata: un utente con ruolo
`key-issuer` può ruotare una chiave di service principal con scope `management`,
ricevere il nuovo plaintext e usarlo per operazioni di management, aggirando il
permesso `service-principals:manage` richiesto dall'endpoint di emissione originale.

La rotazione introduce inoltre due regressioni nel kill switch degli utenti: cambia
il proprietario delle personal key ruotate e lascia attiva fino a un'ora la chiave
vecchia quando il proprietario viene disattivato. La nuova cancellazione utenti
permette infine all'admin corrente di eliminare se stesso, anche quando è l'ultimo
platform admin.

Sul piano funzionale, il rate limit per key è saltato da embeddings e images; gli
inviti rendono il team non cancellabile e un `team_id` inesistente produce un 500;
l'admin UI tronca tutte le collezioni alla prima pagina da 100 elementi. La UI mette
anche credenziali in due superfici facilmente persistenti: JWT admin in
`localStorage` e invite token nella query string.

Counts: **1 CRITICAL · 4 HIGH · 6 MEDIUM · 2 LOW**.

## Issue summary

| ID | Titolo | Priorità | File coinvolti | Stato |
|---|---|---|---|---|
| ISSUE-001 | `key-issuer` può ruotare una key di service principal e ottenere scope management | critical | `infrastructure/web/teams/controller.py:335-359`; `application/service.py:117-140` | **Fixed** ([#249](https://github.com/carlo99999/LiteStarGateway/pull/249)) |
| ISSUE-002 | La rotazione trasferisce la proprietà della personal key all'operatore | high | `application/service.py:117-140`; `infrastructure/web/teams/controller.py:335-359` | **Fixed** ([#250](https://github.com/carlo99999/LiteStarGateway/pull/250)) |
| ISSUE-003 | La disattivazione utente non revoca subito la vecchia key in grace | high | `application/service.py:139`; `persistence/repository.py:85-95` | **Fixed** ([#250](https://github.com/carlo99999/LiteStarGateway/pull/250)) |
| ISSUE-004 | Il rate limit per API key è bypassabile su embeddings e images | high | `application/completion_service.py:423-451,562-594` | **Fixed** ([#251](https://github.com/carlo99999/LiteStarGateway/pull/251)) |
| ISSUE-005 | L'admin può cancellare se stesso e lasciare la piattaforma senza admin | high | `application/user_service.py:186-204` | **Fixed** ([#252](https://github.com/carlo99999/LiteStarGateway/pull/252)) |
| ISSUE-006 | Qualsiasi invite persistito rende il team non cancellabile | medium | `persistence/team_repository.py:85-101`; `orm.py:112-130` | open |
| ISSUE-007 | Creare un invite per un team inesistente restituisce 500 | medium | `application/user_service.py:160-177`; `persistence/invite_repository.py:20-32` | open |
| ISSUE-008 | L'invite token nella query string finisce in log e history | medium | `ui/src/features/users/InviteUserDialog.tsx:56-59`; `SignupPage.tsx:11-17` | open |
| ISSUE-009 | L'admin UI mostra solo i primi 100 record di ogni collezione | medium | `ui/src/features/*/api.ts` | open |
| ISSUE-010 | Il JWT admin persistito in `localStorage` è leggibile da script same-origin | medium | `ui/src/features/auth/AuthProvider.tsx:12-23` | open |
| ISSUE-011 | La rotazione non è atomica e può lasciare una replacement key orfana | medium | `application/service.py:128-140`; `persistence/repository.py:22-39,75-83` | **Fixed** ([#250](https://github.com/carlo99999/LiteStarGateway/pull/250)) |
| ISSUE-012 | Il rate limiter in-memory non elimina mai i bucket inattivi | low | `infrastructure/rate_limiter.py:27-42` | open |
| ISSUE-013 | La UI trasforma qualsiasi errore budget in “nessun budget” | low | `ui/src/features/teams/api.ts:43-49` | open |

## Resolution status — IN PROGRESS

La remediation è iniziata dal finding critical. **ISSUE-001** è risolta dalla
[#249](https://github.com/carlo99999/LiteStarGateway/pull/249): il rotate risolve prima
la key attiva nel team e, per qualsiasi key associata a un service principal, richiede
anche `SERVICE_PRINCIPALS_MANAGE` prima di emettere la replacement. I test RBAC coprono
gli scope `inference`, `management` e `all`, verificano che il diniego non modifichi la
key e che la replacement autorizzata resti soggetta al kill switch dello SP.

**ISSUE-002**, **ISSUE-003** e **ISSUE-011** sono risolte insieme dalla
[#250](https://github.com/carlo99999/LiteStarGateway/pull/250): la rotazione conserva
l'owner originale e committa replacement, grace e audit in una sola unit of work;
la disattivazione serializza su owner e anticipa anche le revoche future. Una
validazione DB finale impedisce inoltre alle race di autenticazione e telemetria di
accettare credenziali revocate. I test PostgreSQL coprono rotate/deactivate,
rotazioni concorrenti, fast path throttled contro revoke e delete degli SP.

**ISSUE-004** è risolta dalla
[#251](https://github.com/carlo99999/LiteStarGateway/pull/251): embeddings e images
propagano l'ID della key al gate RPM, mentre `_prepare` lo richiede e applica il
limite prima di eventuali strategie di routing fatturabili. I test verificano sia i
due endpoint diretti sia il diniego prima della provider call del judge.

| ID | Priorità | Stato | PR |
|---|---|---|---|
| ISSUE-001 | critical | **Fixed** | [#249](https://github.com/carlo99999/LiteStarGateway/pull/249) |
| ISSUE-002 | high | **Fixed** | [#250](https://github.com/carlo99999/LiteStarGateway/pull/250) |
| ISSUE-003 | high | **Fixed** | [#250](https://github.com/carlo99999/LiteStarGateway/pull/250) |
| ISSUE-004 | high | **Fixed** | [#251](https://github.com/carlo99999/LiteStarGateway/pull/251) |
| ISSUE-011 | medium | **Fixed** | [#250](https://github.com/carlo99999/LiteStarGateway/pull/250) |

## Issues

### ISSUE-001 — `key-issuer` può ruotare una key di service principal e ottenere scope management

**Priorità:** critical
**Stato:** **Fixed** ([#249](https://github.com/carlo99999/LiteStarGateway/pull/249))
**File coinvolti:** `src/litestar_gateway/infrastructure/web/teams/controller.py:335-359`, `src/litestar_gateway/application/service.py:117-140`

**Problema**
L'endpoint di emissione di una key per service principal richiede correttamente
`Permission.SERVICE_PRINCIPALS_MANAGE`, mentre il nuovo endpoint generico di rotate
richiede soltanto `Permission.KEYS_ISSUE`. `rotate_for_team` copia senza distinzione
`scope` e `service_principal_id` dalla key sorgente e restituisce il plaintext della
replacement.

**Impatto verificato**
Un utente nel ruolo `key-issuer` può elencare le key del team, scegliere una key di
service principal con scope `management`, chiamare il rotate e ricevere una nuova
management key. Riproduzione end-to-end: rotate `201`, response con
`scope="management"` e plaintext; il nuovo plaintext ha poi ottenuto `200` su
`GET /teams/{id}/usage`, endpoint management che il JWT del `key-issuer` non può
autorizzare direttamente. È una privilege escalation da ruolo limitato a tutti i
permessi team concessi alle management key.

**Soluzione consigliata**
Autorizzare il rotate in base al tipo della key sorgente: una key legata a un service
principal deve richiedere `SERVICE_PRINCIPALS_MANAGE` (e idealmente passare dal
`ServicePrincipalService`); `KEYS_ISSUE` può restare sufficiente solo per personal
key inference-only. Aggiungere un test RBAC negativo con un attore `key-issuer`.

---

### ISSUE-002 — La rotazione trasferisce la proprietà della personal key all'operatore

**Priorità:** high
**Stato:** **Fixed** ([#250](https://github.com/carlo99999/LiteStarGateway/pull/250))
**File coinvolti:** `src/litestar_gateway/application/service.py:117-140`, `src/litestar_gateway/infrastructure/web/teams/controller.py:335-359`

**Problema**
Il metodo promette una replacement con lo stesso owner, ma il controller passa
`current_user.id` e il service usa quel valore come `created_by`. Se un admin o un
altro key issuer ruota la personal key di Alice, la nuova key risulta appartenere
all'operatore, non ad Alice.

**Impatto verificato**
Dopo una rotazione effettuata dall'admin, la lista key mostra `created_by=admin` per
la replacement. Disattivare Alice revoca solo le key personali con
`created_by=Alice`, quindi la replacement continua ad autenticarsi indefinitamente:
il kill switch documentato per le personal key viene aggirato e l'attribuzione di
ownership/audit è falsata.

**Soluzione consigliata**
Per una rotazione usare sempre `key.created_by`; l'identità dell'operatore resta
nell'audit event, non deve diventare il proprietario della credenziale. Coprire il
caso “Alice emette, admin ruota, Alice viene disattivata”.

---

### ISSUE-003 — La disattivazione utente non revoca subito la vecchia key in grace

**Priorità:** high
**Stato:** **Fixed** ([#250](https://github.com/carlo99999/LiteStarGateway/pull/250))
**File coinvolti:** `src/litestar_gateway/application/service.py:139`, `src/litestar_gateway/infrastructure/persistence/repository.py:85-95`

**Problema**
Il rotate imposta sulla key vecchia un `revoked_at` futuro. Il bulk revoke eseguito
dalla disattivazione utente aggiorna però soltanto righe con `revoked_at IS NULL`,
quindi ignora tutte le key già in grace.

**Impatto verificato**
Disattivando il proprietario subito dopo una rotazione, il vecchio plaintext continua
a ottenere `200 /whoami` fino alla scadenza dell'ora di grace. La disattivazione è
esplicitamente il kill switch per sessioni e personal key; una key compromessa non
deve restare valida per un'ora solo perché era già schedulata.

**Soluzione consigliata**
Il revoke per utente deve anticipare anche le revoche future (`revoked_at IS NULL OR
revoked_at > now`) impostandole a `now`, lasciando intatte solo le key già scadute.

---

### ISSUE-004 — Il rate limit per API key è bypassabile su embeddings e images

**Priorità:** high
**Stato:** **Fixed** ([#251](https://github.com/carlo99999/LiteStarGateway/pull/251))
**File coinvolti:** `src/litestar_gateway/application/completion_service.py:423-451,562-594`

**Problema**
Il nuovo parametro `api_key_id` di `_prepare` è opzionale e solo chat/responses lo
passano. `embeddings()` e `images()` invocano `_prepare` senza l'id, quindi
`UsageMeter._enforce_key_rate_limit` vede `None` e salta il bucket per-key. Il team
limit e il pre-auth limit per IP restano attivi, ma il limite configurato sulla key
non viene applicato.

**Impatto**
Una key con `rate_limit_rpm=1` viene bloccata alla seconda chat request, ma può fare
embeddings o image generations fino al limite globale per-IP (120/min), aggirando il
controllo per-key e il relativo contenimento di costo.

**Soluzione consigliata**
Passare `api_key_id` a `_prepare` in entrambi i metodi e aggiungere una matrice di
test rate-limit su chat, responses, embeddings, images e le due superfici native.

---

### ISSUE-005 — L'admin può cancellare se stesso e lasciare la piattaforma senza admin

**Priorità:** high
**Stato:** **Fixed** ([#252](https://github.com/carlo99999/LiteStarGateway/pull/252))
**File coinvolti:** `src/litestar_gateway/application/user_service.py:186-204`

**Problema**
`set_user_admin` e `set_user_active` proteggono esplicitamente le operazioni self, ma
il nuovo `delete_user` non verifica `actor.id == user_id`. Se l'admin non ha più
membership o key create, può cancellare il proprio account.

**Impatto**
L'unico platform admin può eliminarsi lasciando account non-admin nel database. Il
bootstrap non ricrea l'admin perché `users.count() > 0`, quindi organizzazioni,
credenziali, inviti e governance diventano irraggiungibili senza intervento diretto
sul DB. La UI disabilita il pulsante self-delete, ma la protezione deve stare nel
service/API.

**Soluzione consigliata**
Rifiutare il self-delete e mantenere l'invariante “almeno un platform admin” nella
stessa transazione della cancellazione.

---

### ISSUE-006 — Qualsiasi invite persistito rende il team non cancellabile

**Priorità:** medium
**Stato:** open
**File coinvolti:** `src/litestar_gateway/infrastructure/persistence/team_repository.py:85-101`, `src/litestar_gateway/infrastructure/persistence/orm.py:112-130`

**Problema**
La nuova FK `invite.team_id -> team.id` non specifica `ondelete` e usa quindi il
comportamento predefinito `NO ACTION`; `TeamRepository.delete` elimina tutti i figli
“intrinseci” tranne `InviteModel`. Anche gli invite usati o scaduti restano nella
tabella e continuano a bloccare la delete.

**Impatto verificato**
Dopo un solo `POST /invites`, `DELETE /teams/{id}` arriva fino alla delete SQL e
restituisce 500 `FOREIGN KEY constraint failed`. Poiché non esiste un endpoint di
pulizia invite, quel team non è più cancellabile tramite API.

**Soluzione consigliata**
Definire esplicitamente la lifecycle degli inviti: cancellarli nel repository prima
del team o usare `ON DELETE CASCADE`; in alternativa trattarli come riferimento
bloccante e restituire un 409 gestibile, con un modo per rimuoverli.

---

### ISSUE-007 — Creare un invite per un team inesistente restituisce 500

**Priorità:** medium
**Stato:** open
**File coinvolti:** `src/litestar_gateway/application/user_service.py:160-177`, `src/litestar_gateway/infrastructure/persistence/invite_repository.py:20-32`

**Problema**
`create_invite` non verifica l'esistenza del team prima di inserire la nuova FK e il
repository non traduce l'`IntegrityError` in un errore di dominio.

**Impatto verificato**
Un platform admin che invia un UUID inesistente ottiene 500 con rollback DB invece
di un 404/400. Il caso è stato riprodotto con SQLite+FK; la stessa violazione
referenziale è attesa su Postgres. L'endpoint resta fragile per input stale (per
esempio una UI aperta mentre il team viene eliminato).

**Soluzione consigliata**
Risolvere il team nel service prima di emettere l'invite e restituire
`TeamNotFound`; mantenere comunque una traduzione dell'`IntegrityError` per la race
delete-vs-create.

---

### ISSUE-008 — L'invite token nella query string finisce in log e history

**Priorità:** medium
**Stato:** open
**File coinvolti:** `ui/src/features/users/InviteUserDialog.tsx:56-59`, `ui/src/features/auth/SignupPage.tsx:11-17`

**Problema**
La UI genera link `/ui/signup?token=<bearer>` e la pagina legge il token dalla query.
La request iniziale completa viene normalmente registrata dall'access log di
Uvicorn/reverse proxy e resta nella browser history; eventuali navigazioni esterne
possono inoltre propagarla via `Referer` se la policy cambia.

**Impatto**
Chi può leggere access log o history ottiene una credenziale single-use valida 72
ore e può creare l'account prima dell'invitato. Il token è hashato nel DB, ma la URL
reintroduce il plaintext in superfici persistenti.

**Soluzione consigliata**
Trasportare il token nel fragment (`#token=...`, mai inviato al server) oppure
rimuoverlo immediatamente dalla barra con `history.replaceState` prima di qualsiasi
altra attività; impostare una `Referrer-Policy` restrittiva.

---

### ISSUE-009 — L'admin UI mostra solo i primi 100 record di ogni collezione

**Priorità:** medium
**Stato:** open
**File coinvolti:** `ui/src/features/organizations/api.ts:24-31`, `ui/src/features/teams/api.ts:18-23,34-40,52-58`, `ui/src/features/users/api.ts:17-22`, `ui/src/features/api-keys/api.ts:18-25`

**Problema**
Gli endpoint sono correttamente paginati con default 100, ma tutti i client UI
omettono `limit`/`offset` e non richiedono pagine successive. Le tabelle non mostrano
nemmeno che il risultato è troncato.

**Impatto**
Dal 101° organization/team/user/member/key/modello di usage in poi, i record spariscono
dalla console e non possono essere selezionati per inviti, rotazione, revoke o
amministrazione. Il dato esiste e l'API lo espone, ma l'operatore vede una falsa lista
completa.

**Soluzione consigliata**
Implementare paginazione/infinite query nelle tabelle e nei picker oppure iterare le
pagine fino a esaurimento per i dataset che devono essere completi. Il deferred L7
del Round 2 (mancanza metadata) rende utile aggiungere anche `total/next_offset`.

---

### ISSUE-010 — Il JWT admin persistito in `localStorage` è leggibile da script same-origin

**Priorità:** medium
**Stato:** open
**File coinvolti:** `ui/src/features/auth/AuthProvider.tsx:12-23`, `ui/src/lib/api/client.ts:11-21`

**Problema**
La nuova console salva il bearer JWT del platform admin in `localStorage`. Qualsiasi
script che riesca a eseguire sull'origine (XSS futuro, bundle/dependency compromessa,
estensione con accesso pagina) può leggerlo e inviarlo fuori processo; il token ha
privilegi globali e durata lunga.

**Impatto**
Lo scenario richiede esecuzione JavaScript same-origin, ma in quel caso non c'è
protezione `HttpOnly` e il furto sopravvive a reload/browser restart. Un singolo XSS
nella console diventa compromissione completa del gateway.

**Soluzione consigliata**
Per la console browser usare una session cookie `HttpOnly; Secure; SameSite=Strict`
con protezione CSRF, lasciando i bearer JWT all'uso API/CLI. Come mitigazione
intermedia, memoria/sessionStorage + CSP stretta riducono persistenza e superficie.

---

### ISSUE-011 — La rotazione non è atomica e può lasciare una replacement key orfana

**Priorità:** medium
**Stato:** **Fixed** ([#250](https://github.com/carlo99999/LiteStarGateway/pull/250))
**File coinvolti:** `src/litestar_gateway/application/service.py:128-140`, `src/litestar_gateway/infrastructure/persistence/repository.py:22-39,75-83`

**Problema**
`rotate_for_team` chiama `issue()` (che committa la replacement) e poi `update()`
(secondo commit sulla key vecchia). Un errore/crash fra i due passi non può essere
rollbackato.

**Impatto**
Il client riceve 500 e non riceve il plaintext, ma nel DB resta una key attiva
aggiuntiva mentre la vecchia non entra in grace. Retry successivi generano altre key;
l'inventario e gli audit non rappresentano una singola rotazione atomica.

**Soluzione consigliata**
Stage insert e update nella stessa unit of work/transaction e committare una volta;
il plaintext va restituito solo dopo il commit riuscito.

---

### ISSUE-012 — Il rate limiter in-memory non elimina mai i bucket inattivi

**Priorità:** low
**Stato:** open
**File coinvolti:** `src/litestar_gateway/infrastructure/rate_limiter.py:27-42`

**Problema**
`_counts` sostituisce il bucket solo quando la stessa chiave torna a fare traffico,
ma non rimuove mai team/key che non verranno più usati. Revoche, rotazioni e delete
non notificano il limiter.

**Impatto**
Nel fallback single-process senza Redis, un gateway long-lived con churn di API key
accumula una entry per ogni key/team limitato mai visto. La memoria cresce con il
numero storico, non con i caller attivi come afferma il commento.

**Soluzione consigliata**
Pruning periodico/lazy dei bucket vecchi o cache TTL bounded; il path Redis è già
corretto perché usa `EXPIRE`.

---

### ISSUE-013 — La UI trasforma qualsiasi errore budget in “nessun budget”

**Priorità:** low
**Stato:** open
**File coinvolti:** `ui/src/features/teams/api.ts:43-49`

**Problema**
`getTeamBudget` restituisce `null` per qualsiasi `error || !data`, non solo per il
404 `BudgetNotFound`. Un 401/403, un 500 o un errore di rete diventano
indistinguibili da “nessun budget configurato”.

**Impatto**
Durante un problema auth/DB la pagina team comunica un'informazione di governance
falsa e non offre retry/error context all'operatore.

**Soluzione consigliata**
Mappare a `null` soltanto la response 404 e rilanciare gli altri errori affinché
React Query mostri lo stato di errore.

## Verifiche eseguite

- `uv run pytest -q` → **807 passed**.
- `uv run ruff check src tests` → clean.
- `uv run pyrefly check` → 0 errori.
- `uv run pre-commit run --all-files --show-diff-on-failure` → tutti gli hook passati.
- `uv run pip-audit` → nessuna vulnerabilità nota.
- `pnpm lint && pnpm build` in `ui/` → clean (solo warning Vite sul chunk >500 kB).
- Riproduzione API della privilege escalation `key-issuer` → management key.
- Riproduzione API owner transfer + entrambe le key ancora valide dopo deactivate.
- Riproduzione API invite su team inesistente → 500 FK.
- Riproduzione API delete team dopo invite → 500 FK.

## Category scores (this round)

| Categoria | Score | Sintesi |
|---|---:|---|
| Security / RBAC | **6/10** | Core storico solido, ma il rotate apre una escalation concreta e tre regressioni sui kill switch. |
| Money / rate limiting | **7/10** | Team/key limiter ben strutturati, ma il per-key non copre embeddings/images. |
| Persistence / lifecycle | **6.5/10** | FK e UoW generalmente curate; invite lifecycle e rotate multi-commit rompono due workflow reali. |
| Admin UI | **6.5/10** | Build typed e pulita, ma token storage, pagination e masking errori non sono production-ready. |
| Test / CI | **8.5/10** | 807 test verdi e gate forti; mancano test di interazione RBAC/rotation/FK che avrebbero catturato i finding principali. |

**Overall: 6.8/10.** La base resta buona e verificata. ISSUE-001 è stata chiusa dalla
[#249](https://github.com/carlo99999/LiteStarGateway/pull/249); ISSUE-002, ISSUE-003 e
ISSUE-011 dalla [#250](https://github.com/carlo99999/LiteStarGateway/pull/250), e
ISSUE-004 dalla [#251](https://github.com/carlo99999/LiteStarGateway/pull/251). Il
finding high successivo è ora la governance dell'ultimo admin.

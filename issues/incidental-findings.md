# Finding incidentali — trovati costruendo, non in review

[← Index](INDEX.md)

Questo file raccoglie i difetti emersi **fuori** da un round di review: mentre si
implementava una feature, si eseguiva un gate, o si scriveva un test per
qualcos'altro. Non è un round — non c'è stato un perimetro dichiarato né lenti
indipendenti — quindi non ha punteggi di categoria né un executive summary.

Gli ID continuano lo spazio numerico dei round (Round 15 ha chiuso a ISSUE-052),
così un riferimento incrociato resta univoco in tutto `issues/`.

## Issue summary

| ID | Titolo | Severity | File | Status |
|---|---|---|---|---|
| ISSUE-053 | Un `api_base` con host non risolvibile è un 500, non un 400 | MEDIUM | `application/credential_service.py` | Fixed |
| ISSUE-054 | `just migration-check` è ineseguibile in locale per policy di progetto | LOW | `justfile` | Open |

## Findings

### ISSUE-053 — Un `api_base` con host non risolvibile è un 500, non un 400 (MEDIUM)

**Dove.** `src/litestar_gateway/application/credential_service.py`,
`_validate_egress`: la chiamata a `resolve_allowlisted_addresses` era protetta
solo da `except ValueError`.

**Problema.** `resolve_allowlisted_addresses` → `_host_addresses` →
`_resolve_host_addresses` → `socket.getaddrinfo`, che su un nome che non risolve
solleva `socket.gaierror`. `gaierror` è una sottoclasse di **`OSError`**, non di
`ValueError`, quindi non veniva catturata e usciva dal servizio come errore non
mappato → **500**.

**Perché è un problema.** Il percorso è raggiungibile senza alcuna condizione
esotica: basta creare o aggiornare una credenziale `openai_compatible` il cui
host è autorizzato **per nome** nell'allowlist ma in quel momento non risolve —
un server self-hosted spento, un record DNS non ancora propagato, un typo nel
nome. L'operatore riceve un errore server per una misconfigurazione che potrebbe
correggere da sé, e il messaggio non dice cosa è andato storto. Tutti gli altri
esiti di quella validazione sono 400 con un messaggio azionabile: questo era
l'unico a rompere il contratto.

**Impatto verificato.** **Confirmed.** Emerso costruendo una feature che aveva la
stessa forma di validazione egress: i suoi test hanno colpito `gaierror` invece
dell'eccezione di dominio attesa, perché usavano un nome che non risolve su un
portatile. Il percorso credenziali era identico riga per riga.

**Correzione.** Aggiunto `except OSError`, che mappa il fallimento di risoluzione
allo stesso 400 di un target fuori allowlist, con il nome dell'host nel messaggio.

**Nota.** La feature durante la quale il difetto è emerso è stata poi rimossa; la
correzione **no**, perché il difetto era sul percorso credenziali e non su quello.
Il test di regressione qui sotto è stato aggiunto in quel momento, per non lasciare
la correzione sopravvissuta senza copertura.

**Regressione.** `tests/credentials/test_credential_egress.py::`
`test_an_unresolvable_api_base_host_is_a_bad_request_not_a_server_error`.

**Nota di metodo.** Nessuna lente della review del Round 15 ha guardato questo
caso, e non è una mancanza delle lenti: si vede solo eseguendo il codice in un
ambiente dove il nome non risolve. È un argomento a favore dei test che usano
nomi non risolvibili di proposito, invece di patchare sempre il resolver.

### ISSUE-054 — `just migration-check` è ineseguibile in locale per policy di progetto (LOW)

**Dove.** `justfile`, ricetta `migration-check`: esegue
`litestar ... database check` contro il `DATABASE_URL` configurato.

**Problema.** In locale quel `DATABASE_URL` è il database di sviluppo
(`api_keys.db`), che per policy di progetto non va migrato né cancellato. Quindi
la ricetta è in una posizione impossibile: se il DB locale è all'head precedente
risponde `Target database is not up to date` e fallisce, e portarlo all'head
significa violare la policy.

**Perché è un problema.** È un gate che i ground rules di Plan 17, 19 e 20
prescrivono per ogni PR che tocca Alembic, e che in pratica **nessuno può
eseguire** senza infrangere una regola o migrare un DB che non va toccato. Un
gate che si impara a saltare smette di essere un gate, e le sue eventuali
segnalazioni vere si perdono nel rumore.

**Impatto verificato.** **Confirmed** mentre si aggiungeva una migrazione: la
ricetta è fallita con `Target database is not up to date` mentre la migrazione
era corretta. La stessa
proprietà è stata verificata a mano contro un SQLite usa-e-getta (catena
applicata dall'head precedente al nuovo, `database check` → *"No new upgrade
operations detected"*, downgrade che rimuove le sole tabelle nuove).

**Correzione suggerita.** Una ricetta `migration-check-ephemeral` che rispecchi
quello che `test-postgres` già fa per i test: crea un database temporaneo,
applica l'intera catena, esegue `database check`, verifica il downgrade di un
passo e lo distrugge. Il `migration-check` attuale resta utile in CI, dove il
`DATABASE_URL` è effimero per costruzione. In questo modo il gate diventa
eseguibile in locale senza toccare il DB di sviluppo, e il test plan di una PR
può spuntarlo onestamente invece di spiegare perché è stato saltato.

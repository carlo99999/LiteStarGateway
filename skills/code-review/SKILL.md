Esegui una code review completa, profonda, avversariale e verificata del progetto:

"/Users/c.piccinin.ext/Desktop/SideQuests/Litestar test"

L’obiettivo non è produrre una lista generica di possibili miglioramenti, ma individuare difetti reali e dimostrabili nel codice attuale: vulnerabilità, errori di autorizzazione, problemi di correttezza, race condition, inconsistenze transazionali, errori di fatturazione o contabilizzazione, problemi async/concurrency, resource leak, regressioni funzionali, divergenze tra ambienti, problemi UI/API e debito architetturale concreto.

Salva il risultato in:

<PERCORSO_OUTPUT>/round-<N>.md

Metodo di revisione

Prima di cercare problemi:

1. Leggi la documentazione del progetto, la struttura del repository, la configurazione, le migration, i test e le convenzioni architetturali.
2. Identifica lo stack, i confini architetturali, i principali flussi applicativi e gli invarianti di sicurezza e business.
3. Individua il range di commit o le funzionalità introdotte dall’ultima review, quando disponibile.
4. Leggi tutti i precedenti report presenti in <DIRECTORY_REVIEW_PRECEDENTI> e costruisci un elenco dei finding già segnalati, risolti, differiti, considerati by design o esplicitamente confutati.
5. Non riportare nuovamente un problema già presente nei round precedenti, salvo che:
    * la correzione sia incompleta;
    * il problema sia stato realmente riaperto;
    * una nuova superficie presenti la stessa classe di vulnerabilità;
    * emerga un impatto nuovo e distinto.

In questi casi cita chiaramente il finding precedente e spiega perché il problema è ricomparso o non è stato completamente risolto.

Revisori paralleli

Esegui più analisi indipendenti, preferibilmente in parallelo, assegnando a ciascun revisore una lente distinta. Adatta le aree al progetto, ma copri almeno:

1. Sicurezza, autenticazione, autorizzazione, RBAC, multi-tenancy, gestione dei segreti e data exposure.
2. Correttezza Python, async, cancellation, concorrenza, race condition, lifecycle e resource management.
3. Persistenza, SQL, migration, foreign key, transazioni, consistenza e differenze tra database.
4. Business logic, billing, budget, quote, rate limit, accounting e osservabilità.
5. Architettura, dipendenze tra layer, manutenibilità, complessità e duplicazione.
6. API, integrazioni esterne, adapter, error mapping, timeout, retry e streaming.
7. Frontend e TypeScript, quando presenti: stato degli errori, sicurezza delle credenziali, paginazione, form, UX ingannevole e divergenze rispetto alle API.
8. Test, CI/CD, configurazione di produzione, container, dependency security e operational readiness.
9. Analisi avversariale cross-feature: combina funzionalità singolarmente corrette per cercare escalation, bypass e stati incoerenti.

Non fidarti automaticamente dei finding prodotti dai singoli revisori.

Obbligo di verifica

Ogni finding deve essere verificato direttamente sul codice corrente prima di essere incluso.

Per ciascun candidato:

1. Apri e leggi tutte le funzioni coinvolte, non soltanto la riga segnalata.
2. Segui il flusso end-to-end attraverso controller, service, domain, repository, adapter e database.
3. Controlla chiamanti, chiamati, middleware, dependency injection, configurazione e test.
4. Verifica che non esista già una protezione in un altro layer.
5. Controlla il comportamento reale delle librerie esterne tramite il codice installato o la documentazione ufficiale, quando necessario.
6. Riproduci empiricamente il problema quando possibile:
    * test automatico mirato;
    * script minimale;
    * chiamata tramite test client;
    * simulazione di race o cancellation;
    * verifica SQL;
    * ispezione della richiesta realmente inviata a un provider.
7. Distingui chiaramente:
    * comportamento confermato;
    * inferenza fortemente supportata;
    * rischio teorico non riproducibile.
8. Non inserire finding puramente ipotetici, stilistici o basati soltanto su grep.

Ogni finding incluso deve citare almeno un riferimento preciso nel formato file:line o file:start-end.

Baseline tecnica

Prima della review esegui, se disponibili:

* test completi;
* lint;
* type checking;
* formatter/pre-commit;
* test frontend e build;
* audit delle dipendenze;
* controllo delle migration;
* ricerca di segreti tracciati;
* controllo dei file non versionati rilevanti;
* verifica dei confini architetturali;
* eventuali test su database di produzione, non soltanto SQLite.

Riporta esclusivamente risultati realmente eseguiti. Non dichiarare “tutto verde” senza aver lanciato i comandi.

Un test esistente che passa non dimostra che il comportamento sia corretto: valuta anche la qualità e la completezza delle asserzioni.

Severità

Assegna la severità in base a exploitability, impatto verificato, ampiezza e probabilità, non in base alla categoria astratta.

CRITICAL

Usa CRITICAL per problemi come:

* escalation di privilegi concreta;
* bypass completo di autenticazione o autorizzazione;
* esposizione di segreti o dati altamente sensibili;
* possibilità concreta di usare il sistema come relay;
* perdita o manipolazione grave e sistematica di dati;
* bypass economico direttamente sfruttabile e significativo.

HIGH

Usa HIGH per:

* violazioni di sicurezza importanti ma con prerequisiti;
* fatturazione o budget significativamente errati;
* richieste legittime che producono silenziosamente un risultato sostanzialmente scorretto;
* perdita sistematica di osservabilità o accounting;
* race condition ad alto impatto;
* indisponibilità riproducibile sotto carico;
* controlli amministrativi o kill switch inefficaci.

MEDIUM

Usa MEDIUM per:

* problemi reali con impatto circoscritto;
* errori di consistenza o transazionalità;
* casi limite funzionali;
* error handling fuorviante;
* pagination drift;
* audit incompleto;
* configurazioni pericolose ma non immediatamente sfruttabili;
* differenze prod/dev capaci di nascondere errori.

LOW

Usa LOW soltanto per problemi concreti e verificati con impatto limitato, debito tecnico localizzato o hardening preventivo. Non riempire la sezione LOW con preferenze stilistiche.

Formato del report

Usa esattamente questa struttura generale:

Code Review — Round  ()

← Index

Breve introduzione che specifichi:

* il range di commit o le funzionalità analizzate;
* il rapporto con i round precedenti;
* il numero e il tipo di revisori;
* che ogni finding è stato verificato sul codice;
* la baseline tecnica effettivamente eseguita.

Executive summary

Scrivi un riepilogo onesto e specifico.

Indica prima cosa ha retto bene alla revisione, citando controlli realmente effettuati. Non presentare il progetto come scadente quando la baseline è solida.

Spiega poi il tema principale dei nuovi problemi trovati, per esempio:

* governance;
* billing;
* streaming;
* lifecycle;
* UI;
* persistenza;
* autorizzazione;
* regressioni introdotte dalle nuove funzionalità.

Evita frasi generiche come “ci sono alcuni problemi di sicurezza”.

Concludi con:

Counts: **X CRITICAL · X HIGH · X MEDIUM · X LOW**.

Issue summary

Inserisci una tabella:

ID	Title	Severity	Files	Status
ISSUE-001	Titolo specifico	critical/high/medium/low	file.py:10-30	Open

I titoli devono descrivere il comportamento errato e, quando possibile, l’impatto. Evita titoli vaghi come “problema nel service”.

Findings

Ordina i finding per severità e poi per impatto.

Usa identificatori stabili:

* ISSUE-001, ISSUE-002, ecc.;
* oppure continua la numerazione storica del progetto, se i precedenti round usano ID persistenti.

Per ogni finding usa questa struttura:

ISSUE-XXX — Titolo preciso (severity)

Where.
Elenca file, intervalli di righe, funzioni e componenti coinvolti.

Problem.
Descrivi esattamente cosa fa il codice corrente. Segui il flusso completo e indica perché le protezioni esistenti non sono sufficienti.

Why it is a problem.
Spiega quale invariante, contratto, requisito o aspettativa viene violato. Confronta, quando utile, con un percorso equivalente che implementa correttamente il comportamento.

Verified impact.
Descrivi l’impatto concretamente verificato. Includi la riproduzione eseguita e il risultato osservato.

Quando non è stata possibile una riproduzione completa, scrivi esplicitamente quale parte è stata verificata direttamente e quale parte è un’inferenza.

Suggested fix.
Proponi una correzione concreta e compatibile con l’architettura attuale. Indica:

* il layer corretto in cui applicarla;
* eventuali modifiche a schema o migration;
* semantica transazionale necessaria;
* comportamento atteso;
* test di regressione da aggiungere.

Non proporre refactor enormi quando basta una correzione localizzata. Non prescrivere una soluzione specifica quando esistono decisioni di prodotto ancora aperte: in quel caso presenta chiaramente le alternative e il trade-off.

Resolution status

Se stai revisionando soltanto, usa:

Open

Tabella dei finding ancora da risolvere.

Deferred / product decision

Elenca separatamente gli elementi reali che richiedono una decisione architetturale o di prodotto e che non rappresentano una correzione locale.

Non presentare come defect ciò che è consapevolmente by design.

Se invece ti viene richiesto anche di correggere i problemi:

1. Scrivi prima un test di regressione che fallisce.
2. Implementa la correzione minima.
3. Esegui il test mirato.
4. Esegui l’intera suite e tutti i gate.
5. Aggiorna la tabella con commit o PR e descrizione verificata della soluzione.
6. Rileggi il codice corretto rispetto al finding originale.
7. Non dichiarare “fixed” basandoti soltanto sul fatto che il nuovo test passa.

Verified clean

Aggiungi una sezione con le aree sensibili controllate e risultate corrette, per esempio:

* tenant scoping;
* controllo delle permission;
* query parametrizzate;
* JWT algorithm pinning;
* constant-time comparison;
* lifecycle dei client SDK;
* cancellation-safe settlement;
* secret masking;
* transazioni;
* FK e cascade;
* output DTO;
* gestione di stream e disconnect.

Inserisci soltanto verifiche realmente effettuate e abbastanza importanti da evitare che vengano risegnalate nei round successivi.

Verified and refuted

Documenta i finding candidati che, dopo l’analisi, sono risultati:

* falsi positivi;
* già protetti in un altro layer;
* non riproducibili;
* by design;
* già risolti;
* basati su un’interpretazione errata della libreria.

Per ciascuno spiega brevemente perché è stato escluso.

Questa sezione è importante: impedisce che i round successivi ripetano gli stessi falsi positivi.

Category scores

Assegna un punteggio motivato da 1 a 10 almeno a:

* Security & tenancy
* Correctness
* Async & concurrency
* Persistence & transactions
* Billing / business invariants
* Architecture & maintainability
* Testing
* Operations / production readiness
* Frontend, quando presente

Concludi con un punteggio complessivo e una valutazione sintetica, equilibrata e supportata dai finding.

Vincoli di qualità

* Non modificare il codice durante la fase di review, salvo richiesta esplicita.
* Non creare issue separate: produci un unico file Markdown.
* Non riportare duplicati dei round precedenti.
* Non inventare file, linee, test, comandi, risultati, commit o PR.
* Non considerare una feature sicura soltanto perché richiede autenticazione.
* Non considerare un errore innocuo soltanto perché è un edge case.
* Non segnalare preferenze stilistiche come problemi.
* Non gonfiare la severità.
* Non abbassare la severità per rendere il report più rassicurante.
* Non accettare commenti o documentazione come prova che il codice implementi davvero il comportamento dichiarato.
* Cerca esplicitamente divergenze tra commenti, documentazione, test e implementazione.
* Cerca interazioni tra funzionalità, non soltanto bug isolati.
* Valuta sia under-billing sia over-billing.
* Valuta richieste non-streaming, streaming, errori prima del primo chunk, errori a metà stream, disconnect e cancellation.
* Valuta gli stati creati da rotazione, revoca, disattivazione, cancellazione e ricreazione.
* Controlla race condition check-then-act e mapping errato delle IntegrityError.
* Controlla che UI e API non trasformino errori in valori vuoti, zeri o stati apparentemente validi.
* Controlla tutte le superfici equivalenti: chat, responses, embeddings, images, endpoint nativi e routing interno.
* Ogni conclusione importante deve essere collegata a codice concreto.

Priorità finale

Il report deve privilegiare pochi finding reali, verificati e ad alto valore rispetto a una lunga lista di sospetti.

È accettabile concludere che non esistono nuovi finding CRITICAL o HIGH.

Non è accettabile inventare problemi per rendere il round apparentemente produttivo.
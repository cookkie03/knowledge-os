Ho ricontrollato master, compresi gli ultimi commit del 16 novembre 2026.

C'è effettivamente un aggiornamento importante rispetto alla nostra analisi precedente.

Già implementato
 Nell'ultimo commit (adb4d287, ore 16:01 UTC) hai introdotto una trasformazione centralizzata JSON → Markdown direttamente nel processo di ingestion.
Il commit dichiara esplicitamente:
- Nuovo modulo ingest_json_transform.py.
- Riconoscimento intelligente dei JSON.
- Riconoscimento delle sorgenti conversazionali.
- Trasformazione con frontmatter.
- Rimozione dei parser specifici ridondanti da archive_importer.py.
- Eliminazione del precedente limite di 2.000 caratteri.
Questo cambia una parte importante della proposta: non dobbiamo progettare da zero la trasformazione JSON. Dobbiamo consolidarla nella nuova architettura, preservando ciò che hai appena costruito.

Riferimento: ultimo commit.
1. Cosa ho trovato nella repository aggiornata
La trasformazione JSON è un primo passo, ma non la considererei ancora pronta per diventare l'implementazione definitiva.

Ho individuato cinque problemi:
Componente	Stato attuale
JSON transform	Parziale
Unified ingestion	Non completata
ZIP automatico	Non funzionante nella nuova versione
CLI / MCP separati dai tool	Non implementato
Eliminazione delle duplicazioni	Da completare

JSON: cosa manca
Il nuovo ingest_json_transform.py:
- Riconosce alcuni formati, ma detects_json_source() non viene effettivamente utilizzata dalla trasformazione.
- Inserisce ancora il JSON originale come corpo Markdown, senza una vera normalizzazione strutturale.
- Ha un problema nella costruzione dei percorsi: quando la sorgente è esterna a data_dir, utilizza un percorso assoluto che può far finire il Markdown accanto al file originale invece che nella cache derivata.
- Ha un fallback in render_content() che utilizza una variabile raw non definita.
Inoltre, la nuova integrazione è presente in src/cli/ingest.py, ma non in bin/ingest.py.

Questo è importante: setup.sh installa il launcher basato su bin/ragcli, che continua a richiamare la vecchia implementazione. Quindi non possiamo assumere che il nuovo comportamento sia già quello utilizzato dalla CLI installata.
ZIP: una regressione da correggere
La nuova versione di src/cli/archive_importer.py ha eliminato i parser specifici, ma la funzione di estrazione ZIP richiama zipfile senza importarlo. Inoltre, nella nuova implementazione non sono state preservate tutte le protezioni dell'estrattore precedente.

Non eliminerei quindi le protezioni ZIP già esistenti: recupererei la versione robusta, la isolerei nel parser della pipeline e la sottoporrei a nuovi test.

Riferimenti: JSON transformer, ingestion, archive importer.

2. Decisione sul manifest: un catalogo per KB
Qui prenderei una decisione precisa: un unico Catalog per Knowledge Base, con identità delle occorrenze basata su sorgente e percorso relativo.

Non farei né un manifest indipendente per ogni directory né un unico elenco di percorsi non qualificati.

La repository possiede già quasi esattamente il modello necessario:
KB
└── catalog.json
    ├── sources
    ├── files
    ├── documents
    └── jobs


La chiave dei file è già composta da:
(source_id, relative_path)



Quindi queste due sorgenti possono tranquillamente contenere entrambe notes/readme.md senza conflitti.

È una soluzione migliore del vecchio schema basato su derived/manifests/sync.json e source_<hash>.json.

La proposta è quindi:
- Il Catalog resta la fonte autorevole dello stato.
- Ogni sorgente ha il proprio identificativo stabile.
- Ogni scansione genera un job e uno snapshot temporaneo.
- Il job conserva i dati necessari al recupero dopo un crash.
- Non esistono manifest permanenti separati per ogni directory.
- L'Inbox usa una sorgente logica append-only, con identificativi dei batch.
Riutilizziamo quindi src/storage/catalog.py, evitando di introdurre un secondo sistema di persistenza.

3. Architettura target
Questa è la struttura che adotterei. Rispetta la tua idea di sources/ e tools/, senza creare un'altra gerarchia inutilmente complicata.
3. Architettura proposta

src/
├── tools/                         # Unica implementazione delle operazioni
│   ├── knowledge.py               # KB lifecycle e gestione
│   ├── ingestion.py               # Acquisizione e orchestrazione
│   ├── retrieval.py               # Ask, search, evidence
│   ├── memory.py                  # Persistent memory e dreaming
│   ├── agents.py                  # Agent integrations e hooks
│   ├── mcp.py                     # Registrazione e autenticazione MCP
│   ├── config.py                  # Configurazione
│   └── system.py                  # Doctor e servizi
│
├── sources/                       # Interfacce verso le operazioni
│   ├── cli/
│   │   ├── app.py                 # Entry point argparse
│   │   ├── dashboard.py           # Dashboard iniziale
│   │   ├── output.py              # Formatting e JSON output
│   │   ├── completion.py          # Bash, Zsh, Fish
│   │   └── commands/
│   │       ├── kb.py
│   │       ├── ingest.py
│   │       ├── retrieval.py
│   │       ├── memory.py
│   │       ├── mcp.py
│   │       ├── config.py
│   │       └── system.py
│   │
│   └── mcp/
│       ├── server.py              # FastMCP initialization
│       ├── registrations.py       # Tool declarations e schemas
│       └── auth.py                # MCP authentication middleware
│
├── pipeline/                      # Elaborazione effettiva dei contenuti
│   ├── orchestrator.py
│   ├── source_scanner.py
│   ├── ingestion_worker.py
│   ├── parsers.py
│   ├── json_transform.py
│   ├── archive_parser.py
│   ├── indexer.py
│   └── dreaming.py
│
├── storage/
│   ├── catalog.py
│   ├── inbox.py
│   └── json_store.py
│
└── integrations/
    ├── lightrag.py
    ├── mcp_clients.py
    └── agent_hooks.py



Questa è la separazione delle responsabilità:
Livello	Responsabilità
tools/	Esegue operazioni applicative vere
sources/cli/	Interpreta comandi e presenta risultati
sources/mcp/	Espone le stesse operazioni agli agenti
pipeline/	Scansiona, trasforma, indicizza ed elabora
storage/	Gestisce persistenza e catalogo
integrations/	Comunica con sistemi esterni

Una regola architetturale fondamentale:

Nessuna logica di business dentro CLI o MCP.

Ad esempio:
# CLI adapterdef command_ingest(args):    result = tools.ingestion.run(        kb_ids=args.kb,        wait=args.wait,    )    print_result(result)



# MCP adapter@mcp.tool()def ingest_run(kbs: list[str] | None = None):    return tools.ingestion.run(kb_ids=kbs)



Le due interfacce chiamano lo stesso codice.

Non voglio più vedere KBRouter contenere contemporaneamente implementazioni operative, registrazione dei tool MCP e logica di business come avviene oggi.

4. Unified Ingestion Pipeline
Questa sarà la parte centrale dell'intervento.
4.1 Un solo ingestion engine
Adotterei tre modalità di acquisizione, ma un solo processamento.
Ingestion sources
Sources

Directory persistenti


Inbox

File ricevuti


Primary

Directory principale KB




Ingestion Orchestrator

Scope selection · scanning · job creation · scheduling


Unified Ingestion Worker

File validation · parsing · transformation · indexing


Catalog + LightRAG


Sources
Le directory registrate diventano sorgenti persistenti.

Ogni sorgente possiede:
- Identificativo stabile.
- Percorso assoluto canonico.
- Nome.
- Regole di inclusione/esclusione.
- Ultimo scan completato.
- Politica di cancellazione.
- Stato di disponibilità.
La directory principale della KB diventa semplicemente una sorgente di tipo primary.

Le altre sono di tipo folder.

Non devono più esistere due sistemi indipendenti per registrarle: registry.yaml e Catalog.

Il Catalog diventa l'autorità sulle sorgenti.
Inbox
La Inbox smette di avere una propria pipeline.

Mantiene soltanto:
- Ricezione sicura di file e directory.
- Staging temporaneo.
- Queue dei batch.
- Stati pending, processing, processed e failed.
- Archiviazione dei file originali elaborati.
Il suo contenuto viene consegnato direttamente all'Ingestion Orchestrator.

Vorrei mantenere una sorgente logica inbox per KB, con i percorsi relativi organizzati per batch. Questo permette di conservare provenienza, deduplicazione e riferimenti ai file originali senza creare una sorgente differente per ogni upload.

Non deve più esistere consume_inbox() come motore di processamento autonomo.

La ricezione di un batch crea un normale job di ingestion. Il worker comune lo prende in carico automaticamente.

Non servono link.consume, link.ingest o altri interruttori.
Job management
Manteniamo il sistema di job, che è utile anche senza sincronizzazione remota.

I job devono avere:
- ID e idempotency key.
- KB e source associati.
- Stato.
- Contatori di file.
- Errori e warning.
- Possibilità di retry.
- Informazioni sufficienti per recovery dopo crash.
Le fasi remote come TRANSFERRING non servono più. Il workflow va semplificato attorno a scansione, staging e processing locale.

4.2 Manifest e scansioni
Riutilizzerei le capacità già presenti nel Catalog.

Non cancellerei però ciecamente tutte le funzioni di src/sync/client.py: le sue implementazioni di snapshot, checksum, rilevazione delle modifiche e rename sicuri sono utili anche per la scansione locale.

Le trasferirei in pipeline/source_scanner.py, eliminando la terminologia e la dipendenza dal protocollo remoto.

Il nuovo flusso:
1. Scansiona una sorgente.
2. Calcola gli hash.
3. Confronta i file con il Catalog.
4. Pianifica add, modify, rename e delete.
5. Verifica che la scansione sia completa.
6. Crea un job interno.
7. Il worker applica le modifiche.
8. Aggiorna il Catalog.
Le cancellazioni continueranno a essere soggette ai controlli già presenti: una scansione incompleta non deve mai autorizzare la rimozione dei documenti mancanti.

Questa è una condizione non negoziabile.

5. Pipeline di parsing e trasformazione
La pipeline dovrebbe decidere autonomamente che cosa fare di ogni file.
Categoria	Trattamento
Text / Markdown / Code	Lettura e normalizzazione
JSON	Parsing strutturale e Markdown transformation
JSONL	Elaborazione record per record
ZIP	Estrazione sicura e ricorsione sui contenuti supportati
PDF	Docling, estrazione e OCR
Office / EPUB / Email	Docling
Images	Vision / OCR
Audio / Video	Whisper / ASR e parsing multimodale

JSON
Sposterei integralmente ingest_json_transform.py in src/pipeline/.

Il parser dovrebbe:
1. Validare il JSON.
2. Determinare la struttura e il tipo di sorgente.
3. Generare una rappresentazione Markdown leggibile e completa.
4. Preservare i metadati rilevanti.
5. Salvare il derivato nella cache.
6. Consegnarlo all'indexer comune.
Non farei una collezione di importer ChatGPT, Claude, Gemini e Notion separati.

Userei un parser strutturale generico con strategie di riconoscimento per le strutture più note.

Per gli export conversazionali, è importante preservare ordine dei messaggi, ruoli, timestamp, identificativi e provenienza. Non basta aggiungere cinque righe YAML davanti al JSON originale.

Il contenuto completo deve essere preservato: niente troncamenti arbitrari.

Le preferenze estratte dagli archivi, invece, dovrebbero diventare eventualmente memory candidates sottoposte a revisione, non memorie permanenti attivate automaticamente.
ZIP
Aggiungerei .zip alla pipeline come categoria archive.

L'archivio diventerebbe un contenitore di contenuti, non una procedura di importazione separata.

Per esempio:
conversations.zip
└── conversations.json
    └── JSON parser
        └── Markdown document
            └── LightRAG


L'estrattore dovrebbe essere interno alla pipeline e applicare:
- Protezione Zip Slip.
- Blocco symlink e percorsi assoluti.
- Limiti di dimensione per file.
- Limiti di dimensione totale.
- Compression ratio massimo.
- Limite di profondità degli archivi annidati.
- Controllo dei percorsi generati.
Gli elementi estratti dovrebbero essere elaborati in staging, senza inquinare la directory sorgente dell'utente.

Come provenienza, conserverei un riferimento del tipo:
conversations.zip!/conversations.json


Così possiamo mantenere la relazione tra file originale e documento derivato.
URL
Rimozione completa
Eliminerei:
- bin/url_importer.py.
- src/cli/url_importer.py.
- Le funzioni import_media_url_to_kb e ingest_url.
- ragcli import-url.
- ragcli retrieve url.
- MCP ingest_url.
- tests/test_url_importer.py.
- La dipendenza da yt-dlp, dove non utilizzata da altro codice.
Whisper e il parsing locale di audio/video restano intatti.

Non stiamo eliminando la capacità multimodale: eliminiamo soltanto il downloader da piattaforme esterne.

6. Eliminazione di Sync e Link
Qui farei una pulizia vera, non un semplice rename dei comandi.
Elemento attuale	Intervento
src/sync/client.py	Spostare snapshot e diff nel source scanner, eliminare il resto
src/sync/server.py	Eliminare
Remote manifest protocol	Eliminare
ragcli sync	Eliminare
ragcli job	Spostare sotto ingest jobs
source_ingest MCP	Sostituire con ingestion comune
link on/off/status	Eliminare
link nel registry	Migrare e rimuovere
Campi SSH / remote origin	Eliminare dal modello dopo migrazione

È importante non confondere la cancellazione del protocollo remoto con la cancellazione delle funzionalità di scansione locale.

Manteniamo hash, rename detection, delta, safety guards, job tracking e recovery.

Queste funzioni sono parte del motore di ingestion, non della sincronizzazione tra dispositivi.

7. CLI: organizzazione finale
Vorrei mantenere una gerarchia breve e prevedibile.
Area	Comandi
KB	kb create, list, show, start, stop, delete, move
Ingestion	ingest run, status, jobs
Sources	ingest sources add, list, remove
Inbox	ingest inbox add, status, list
Retrieval	ask, retrieve context, retrieve evidence
Memory	memory add, search, list, deprecate, dream
MCP	mcp add, remove, status, auth
Agents	agent hooks install, inject, capture
System	system doctor, services status, restart, logs
Config	config show, set, models

Esempi della nuova esperienza:
# Scansiona tutte le KB e tutte le sorgenti
ragcli ingest run

# Una KB specifica
ragcli ingest run --kb engineering

# Invia file ricevuti direttamente alla pipeline
ragcli ingest inbox add ~/Downloads/report.pdf --kb research

# Registra una directory persistente
ragcli ingest sources add ~/projects/docs --kb engineering

# Query cross-KB
ragcli ask "What are my current architecture decisions?"

# Ricerca memoria cross-KB
ragcli memory search "database design"

# Visualizza job
ragcli ingest jobs list

# Controlla autenticazione MCP
ragcli mcp auth status


7.1 Default e scope
Adotterei questa matrice:
Operazione	Default senza --kb
KB list / status	Tutte
Ingest run	Tutte
Ingest status / jobs	Tutte
Ask / retrieval	Tutte
Memory search / list	Tutte
Doctor	Tutto il sistema
Service status	Tutti
Ingest inbox add	KB predefinita se univoca, altrimenti selezione
Source registration	KB predefinita se univoca, altrimenti selezione
Memory add	KB esplicita o selezione
KB delete	Selezione esplicita
MCP auth	Configurazione globale

Questa distinzione evita un errore: non tutto ciò che non specifica una KB deve scrivere su tutte le KB. Per le ricerche è corretto; per un upload o una cancellazione potrebbe produrre effetti indesiderati.

Quando manca un dato non deducibile, il CLI dovrebbe proporre una scelta interattiva, non fallire con un traceback.

8. Dashboard iniziale
ragcli senza argomenti deve diventare la home dell'applicazione.

Una prima implementazione dovrebbe mostrare:
╭────────────────── KNOWLEDGE OS ──────────────────╮
│                                                   │
│ SYSTEM                                            │
│ Runtime                  HEALTHY                   │
│ LightRAG                 RUNNING                   │
│ Ollama                   CONNECTED                 │
│ MCP                      RUNNING                   │
│                                                   │
│ KNOWLEDGE BASES                                   │
│ engineering              ● Running                 │
│ research                 ● Running                 │
│ personal                 ● Stopped                 │
│                                                   │
│ INGESTION                                         │
│ Pending jobs              3                        │
│ Processing                1                        │
│ Failed                    0                        │
│                                                   │
│ RECENT ACTIVITY                                   │
│ research   12 files processed   14:32              │
│ engineering 4 files updated     13:51              │
│                                                   │
│ QUICK ACTIONS                                     │
│ [1] Ask knowledge                                  │
│ [2] Run ingestion                                  │
│ [3] Send files to inbox                            │
│ [4] Manage knowledge bases                         │
│ [5] System diagnostics                             │
╰───────────────────────────────────────────────────╯


Non introdurrei inizialmente una TUI pesante con dipendenze aggiuntive.

Una dashboard testuale interattiva è sufficiente. In modalità non-interattiva il comando deve comunque produrre un riepilogo leggibile.

9. MCP e autenticazione
MCP come adapter
Un solo server multi-KB, registrato una volta per client, è la direzione che adotterei.

Le dichiarazioni FastMCP diventano semplici wrapper dei tool centralizzati.

Eliminerei:
- La doppia implementazione src/mcp/router.py e src/mcp/kb_router.py.
- Le funzioni operative duplicate.
- Il vecchio ingest_url.
- inbox_consume come operazione di processamento indipendente.
- source_ingest come secondo workflow.
Resta un unico catalogo di operazioni MCP.
Autenticazione
Comandi:
ragcli mcp auth status
ragcli mcp auth set
ragcli mcp auth generate
ragcli mcp auth rotate
ragcli mcp auth disable


Comportamento:
- set utilizza un prompt nascosto o stdin, mai una password nell'argomento.
- status mostra lo stato senza rivelare il segreto.
- generate genera un token sicuro.
- rotate sostituisce il token e riavvia il servizio se necessario.
- disable disabilita esplicitamente la protezione.
Il middleware manterrà un meccanismo di autenticazione semplice tramite Bearer token e, dove necessario, X-API-Key.

Eliminerei l'autenticazione tramite query parameter: un segreto non deve finire negli URL e nei log.

10. Piano dettagliato di implementazione
Organizzerei il lavoro in otto fasi, con dipendenze e criteri di completamento. Non farei modifiche distruttive prima di aver consolidato il nuovo percorso operativo.
Fase 0 · Baseline e salvaguardia
P0

Obiettivo: conoscere e preservare lo stato funzionante.
- Creare branch dedicato e tag/backup del commit iniziale.
- Eseguire e registrare il risultato dell'intera suite pytest.
- Verificare quale ragcli viene effettivamente eseguito da PATH.
- Inventariare servizi systemd, MCP client registrations e configurazioni.
- Fotografare stato di registry, cataloghi, manifest legacy e Inbox.
- Identificare tutti gli import interni ai vecchi moduli bin.*.
Gate: baseline riproducibile, backup verificato, nessun dato persistente modificato.

Fase 1 · Consolidamento architettura
P0

Obiettivo: stabilire una sola implementazione canonica.
- Creare src/tools/.
- Creare src/sources/cli/.
- Creare src/sources/mcp/.
- Definire contratti e risultati standard delle funzioni tool.
- Spostare le operazioni KB, retrieval, memory e system nei tool centralizzati.
- Rendere CLI e MCP dipendenti esclusivamente dai tool.
- Consolidare pyproject.toml e gli import Python.
- Correggere le risoluzioni di ROOT, HOME, KB_ROOT, CONFIG e INGEST_SCRIPT.
- Rendere il root launcher e quello installato in ~/.local/bin due richiami allo stesso entry point.
- Eliminare progressivamente le copie duplicate di ragcli.py, kb_registry.py e dei manager.
Gate: una singola funzione operativa è eseguibile tramite CLI e MCP, con gli stessi risultati e senza duplicazione.

Fase 2 · Catalogo e data model
P0

Obiettivo: un'unica fonte di verità.
- Introdurre una versione esplicita dello schema del Catalog.
- Semplificare le Source records: primary, folder, inbox.
- Sostituire origin_host e origin_folder con metadati di percorso locale.
- Centralizzare la registrazione delle sorgenti.
- Trasferire le external sources dal registry YAML al Catalog.
- Eliminare la duplicazione tra registry.yaml e Catalog.
- Conservare la chiave composta (source_id, relative_path).
- Centralizzare tracking di documenti, file, job, hash e revisioni.
- Eliminare i manifest legacy persistenti come fonte autorevole.
- Mantenere snapshot temporanei per job e recovery.
Gate: tutte le sorgenti di una KB sono rappresentate nel medesimo Catalog e non esistono collisioni tra percorsi relativi.

Fase 3 · Unified Ingestion Engine
P0

Obiettivo: un solo processo di elaborazione.
- Implementare pipeline/source_scanner.py.
- Trasferire snapshot, hash e diff sicuri dal vecchio sync.client.
- Implementare un unico pipeline/orchestrator.py.
- Riutilizzare e adattare IngestionWorker.
- Mantenere single-writer locks e checkpoint recovery.
- Unificare gestione retry, errori e job status.
- Trasformare Inbox in un semplice intake queue.
- Creare una sorgente logica append-only per Inbox.
- Far confluire Inbox, primary e external sources nello stesso worker.
- Mantenere i file originali Inbox nell'archivio processed, con provenienza associata.
- Eliminare la vecchia funzione consume_inbox che inserisce direttamente contenuti in LightRAG.
- Garantire che Inbox e directory persistenti utilizzino esattamente lo stesso parser e indexer.
Gate: lo stesso file, acquisito da primary source, external source o Inbox, produce lo stesso trattamento e le stesse garanzie di integrità.

Fase 4 · Unified parsing
P1

Obiettivo: supporto automatico dei formati.
- Spostare la trasformazione JSON in src/pipeline/.
- Correggere normalizzazione, percorsi, frontmatter e idempotenza.
- Usare hash del contenuto e versione del parser per identificare i derivati.
- Implementare parsing strutturale JSON e JSONL.
- Preservare metadati e formati conversazionali riconosciuti.
- Implementare archive_parser.py per ZIP.
- Trasferire e rafforzare l'estrattore ZIP sicuro.
- Gestire archivi annidati con limiti.
- Generare documenti figli con provenance dell'archivio originale.
- Conservare i parser PDF, Office, OCR, immagini, audio e video.
- Rimuovere completamente il downloader URL e i relativi package non più necessari.
Gate: JSON ordinario, JSON conversazionale, ZIP contenente JSON, PDF, Office, immagini e audio attraversano tutti lo stesso dispatcher e l'indexer comune.

Fase 5 · Rimozione Sync e Link
P1

Obiettivo: eliminare i sottosistemi superflui dopo averne sostituito le responsabilità.
- Eliminare src/sync/server.py.
- Eliminare il protocollo remoto, manifest SSH e relativi handler.
- Eliminare il comando sync.
- Trasferire job in ingest jobs.
- Eliminare flag link.consume e link.ingest.
- Rimuovere gli handler link on/off/status.
- Migrare i batch Inbox e le configurazioni esistenti.
- Eliminare i riferimenti remoti e i relativi test.
- Rimuovere i vecchi script ingest duplicati dopo aver verificato la parità.
Gate: nessun workflow dipende più da Sync remoto o Link, ma tutte le funzionalità locali di scanning, retry e job tracking sono operative.

Fase 6 · CLI e MCP adapters
P1

Obiettivo: completare le due interfacce.
- Implementare il nuovo parser CLI gerarchico.
- Esporre i tool centralizzati tramite FastMCP.
- Consolidare i due router MCP in un solo server multi-KB.
- Semplificare la registrazione MCP nei client.
- Eliminare i tool manuali di importazione.
- Introdurre i tool per submission, run e status dell'ingestion comune.
- Implementare ragcli mcp auth e relative operazioni.
- Separare autenticazione, presentazione e logica applicativa.
- Conservare la compatibilità delle configurazioni MCP client dove possibile.
Gate: nessuna operazione applicativa è duplicata tra CLI e MCP.

Fase 7 · Human UX
P2

Obiettivo: rendere la CLI piacevole da usare quotidianamente.
- Dashboard senza argomenti.
- Scope predefiniti cross-KB.
- Selettori interattivi per operazioni che necessitano di una KB.
- Conferme per operazioni distruttive.
- Output coerente e --json globale.
- Autocomplete Bash, Zsh e Fish.
- Messaggi di errore e suggerimenti contestuali.
- Help completo e localizzazione coerente.
Gate: un utente può usare le operazioni comuni senza consultare continuamente la documentazione.

Fase 8 · Migration, testing e release
P2

Obiettivo: passaggio sicuro alla nuova architettura.
- Migration script idempotente per Catalog.
- Dry-run e report dei dati da migrare.
- Importazione delle sorgenti dal registry.
- Reconciliation tra vecchi manifest, Catalog e LightRAG.
- Migrazione Inbox pending e failed; gestione prudente dei batch processing/processed.
- Preservazione dei documenti già indicizzati.
- Test di regressione CLI e MCP.
- Test end-to-end con ingestion reale su Debian.
- Verifica dell'installazione pulita da clone.
- Aggiornamento README, user guide, glossary e ADR.
- Rimozione definitiva delle implementazioni legacy.
Gate: installazione pulita, dati conservati, suite completa verde e un unico entry point operativo.


11. Strategia di migrazione dei dati: punto delicato
Non farei una migrazione distruttiva dei Catalog.

Ci sono tre sistemi di stato da riconciliare:
1. Il vecchio manifest di bin/ingest.py.
2. Il nuovo Catalog storage/catalog.py.
3. Lo stato effettivamente presente in LightRAG.
In più, l'Inbox precedente ha indicizzato file usando identificativi inbox:..., ma non necessariamente ha registrato le stesse occorrenze nel Catalog.

La migrazione dovrà quindi:
- Backup di ogni Catalog e manifest precedente.
- Registrazione delle vecchie sorgenti con ID stabili.
- Confronto dei checksum con i documenti già indicizzati.
- Riconoscimento dei documenti già presenti, evitando re-embedding inutili.
- Conservazione dei file originali Inbox.
- Nessuna cancellazione automatica di documenti durante la fase di riconciliazione.
- Report finale delle occorrenze non riconciliate.
Anche le operazioni force dovranno essere rivalutate: una rigenerazione dei derivati non dovrebbe comportare automaticamente cancellazioni non necessarie o nuova indicizzazione di contenuti identici.

12. Test e criteri finali di accettazione
Considererei la riorganizzazione completata soltanto quando questi scenari funzionano.
Scenario	Risultato atteso
Aggiungo un PDF alla primary source	Viene indicizzato
Modifico il PDF	Viene aggiornata la versione
Rinomino un file	Rename riconosciuto senza re-embedding
Aggiungo una external source	Entra nello stesso processo
Invio un file all'Inbox	Viene elaborato dallo stesso worker
Invio un ZIP con JSON	I contenuti vengono estratti e indicizzati
Invio un JSON conversazionale	Viene normalizzato automaticamente
Invio un JSON molto grande	Nessuna perdita arbitraria di contenuto
Una sorgente è irraggiungibile	Nessuna cancellazione accidentale
Un job fallisce	Può essere ripreso o ritentato
Due sorgenti hanno lo stesso percorso relativo	Nessuna collisione
Eseguo ragcli ingest run	Tutte le KB vengono gestite
Eseguo una ricerca senza KB	Ricerca cross-KB
Uso CLI e MCP	Stessa logica e stessi risultati
Elimino una KB	Originali preservati secondo la politica prevista
Ruoto il token MCP	Servizio aggiornato senza esposizione del segreto

Inoltre, introdurrei un test architetturale esplicito:

Nessun file dentro sources/cli/ o sources/mcp/ può importare direttamente l'implementazione dei parser, modificare il Catalog o chiamare l'API LightRAG.

Deve passare dai tool.

13. Priorità e risultato atteso
Sequenza di priorità
P0 · Fondamenta
Bloccante

Tool layer, entry point unico, Catalog centralizzato, unified ingestion.
P1 · Semplificazione
Alta

JSON/ZIP automatici, rimozione URL, Sync e Link, adapter MCP/CLI.
P2 · UX e release
Successiva

Dashboard, defaults, autocomplete, migrazione definitiva e documentazione.
Il risultato architetturale che vogliamo ottenere
Alla fine KnowledgeOS dovrebbe avere:
- Un solo core applicativo.
- Un solo sistema di tool.
- Un solo ingestion engine.
- Un solo Catalog per KB.
- Un solo job management.
- Una sola implementazione per ogni operazione.
- Due interfacce: CLI per le persone e MCP per gli agenti.
E soprattutto, vorrei che sparisse una classe intera di problemi: le stesse funzionalità implementate più volte, con comportamenti leggermente differenti a seconda del punto da cui vengono invocate.

Il lavoro più importante non sarà aggiungere nuove funzionalità, ma fare in modo che ogni funzionalità esistente abbia una sola implementazione affidabile.

La mia raccomandazione è seguire esattamente quest'ordine: prima consolidare il core e l'ingestion, poi eliminare i vecchi workflow e infine costruire la nuova esperienza CLI/MCP. In questo modo non sacrifichiamo la stabilità dei dati per ottenere una repository apparentemente più pulita.
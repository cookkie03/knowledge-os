# Spec — Centralizzare i tool e unificare l'ingestion

Derivata da `docs/gpt-plan.md`, con le decisioni già prese. Le deviazioni dal piano GPT sono marcate **[≠ GPT]**.

## Problem Statement

Chi usa KnowledgeOS ottiene comportamenti diversi a seconda di dove invoca un'operazione. Oggi esistono:

- tre implementazioni del registro KB (`kb_registry`, `kb_manager`, `kb_config`);
- due della memoria (`memory_manager`, `mcp/memories`);
- due pipeline di ingestion (`ingest`, `ingest_pipeline`) più la `consume_inbox` autonoma;
- due router MCP (`router`, `kb_router`);
- due alberi di codice (`bin/*.py` e `src/`); il launcher installato chiama ancora `bin/`.

L'estrattore ZIP nuovo è rotto (manca `import zipfile`, mancano le protezioni). Il transform JSON incolla il JSON grezzo sotto un frontmatter. Le sorgenti vivono sia in `registry.yaml` sia nel Catalog. L'ingestion di una cartella git non rispetta `.gitignore`.

## Solution

Ogni operazione ha **una sola implementazione** in `src/tools/`. CLI e MCP sono adapter sottili sopra quelle funzioni. Le tre modalità di acquisizione (primary, folder, inbox) finiscono nello stesso scanner, nello stesso worker, nello stesso dispatcher di parser e nello stesso indexer. Il Catalog per KB è l'unica autorità su sorgenti, file, documenti e job. Si eliminano i sottosistemi che non servono più: downloader URL, protocollo sync remoto, flag link.

## User Stories

1. Come utente, voglio un solo comando `ragcli` installato, così che il comportamento non dipenda da quale copia del codice viene eseguita.
2. Come utente, voglio che `ragcli kb create` e il tool MCP equivalente producano la stessa KB, così da poter alternare CLI e agenti.
3. Come utente, voglio creare una KB che includa più cartelle esistenti, così da interrogare progetti diversi insieme.
4. Come utente, voglio che una cartella git venga ingerita rispettando tutti i suoi `.gitignore` (anche annidati), così che segreti, `node_modules`, venv e artefatti non entrino nella KB.
5. Come utente, voglio aggiungere pattern di esclusione miei oltre a `.gitignore`, così da escludere cose versionate ma irrilevanti.
6. Come utente, voglio `ragcli ingest run` senza `--kb` per scansionare tutte le KB e tutte le sorgenti.
7. Come utente, voglio `ragcli ingest run --kb X` per limitarmi a una KB.
8. Come utente, voglio che un file modificato venga reindicizzato come nuova versione.
9. Come utente, voglio che un file rinominato venga riconosciuto senza re-embedding.
10. Come utente, voglio che una sorgente irraggiungibile o una scansione incompleta non cancelli mai documenti.
11. Come utente, voglio che due sorgenti con lo stesso percorso relativo non collidano.
12. Come utente, voglio inviare file o ZIP all'inbox con `ragcli ingest inbox add`, e che vengano elaborati dallo stesso worker delle cartelle.
13. Come utente, voglio che uno ZIP venga estratto in staging in modo sicuro (zip slip, symlink, path assoluti, limiti di dimensione, ratio, profondità) senza toccare la cartella sorgente.
14. Come utente, voglio che ogni documento estratto da uno ZIP conservi la provenienza `archivio.zip!/percorso/interno`.
15. Come utente, voglio che un export JSON conversazionale (ChatGPT, Claude, ecc.) diventi Markdown leggibile con ruoli, ordine, timestamp e id preservati.
16. Come utente, voglio che un JSON generico diventi Markdown strutturato, senza troncamenti.
17. Come utente, voglio che JSONL venga elaborato record per record.
18. Come utente, voglio che i derivati (Markdown generati) finiscano sempre nella cache della KB, mai accanto all'originale.
19. Come utente, voglio che PDF, Office, immagini e audio/video continuino a funzionare come oggi.
20. Come utente, voglio vedere i job con `ragcli ingest jobs`, e poter ritentare un job fallito.
21. Come utente, voglio che un job interrotto da un crash sia recuperabile.
22. Come utente, voglio registrare e rimuovere sorgenti con `ragcli ingest sources add|list|remove`.
23. Come utente, voglio che `ask` e `memory search` senza `--kb` cerchino in tutte le KB.
24. Come utente, voglio che le operazioni di scrittura senza `--kb` (inbox add, sources add, memory add) usino la KB predefinita se unica, altrimenti mi chiedano quale, invece di scrivere ovunque o fallire con un traceback.
25. Come utente, voglio che `kb delete` richieda sempre una KB esplicita e una conferma.
26. Come utente, voglio gestire il token MCP con `ragcli mcp auth status|set|generate|rotate|disable` senza che il segreto appaia in argomenti, URL o log.
27. Come agente, voglio un solo server MCP multi-KB, registrato una volta per client.
28. Come agente, voglio tool MCP per submit/run/status dell'ingestion comune, senza tool separati per URL o per consumare l'inbox.
29. Come agente, voglio che ogni tool MCP restituisca gli stessi dati del comando CLI corrispondente.
30. Come manutentore, voglio un test che fallisca se `cli/` o `mcp/` importano parser, Catalog o client LightRAG direttamente.
31. Come manutentore, voglio che i dati delle KB esistenti vengano migrati con dry-run, report e nessuna cancellazione.
32. Come manutentore, voglio che i test importino moduli dal layout `src/` e non da `bin.*`.
33. Come utente, voglio che `ragcli` senza argomenti mostri un riepilogo testuale dello stato (servizi, KB, job).

## Implementation Decisions

### Layout **[≠ GPT]**
Niente `sources/` (collide con il termine di dominio *Source*) e niente `integrations/` (cartella quasi vuota).

- `tools/` — l'unica implementazione delle operazioni. Funzioni semplici che ricevono argomenti espliciti e restituiscono `dict`. Moduli: `kb`, `ingest`, `retrieval`, `memory`, `agents` (hook + registrazione client MCP), `system` (doctor, servizi, config, mcp auth).
- `cli/` — parser argparse, formattazione output, prompt interattivi. Chiama solo `tools`.
- `mcp/` — server FastMCP, dichiarazioni dei tool, middleware auth. Chiama solo `tools`.
- `pipeline/` — scanner, worker, dispatcher dei parser (incluso JSON e archivi), indexer, dreaming.
- `storage/` — Catalog, inbox (solo coda di intake), json store atomico.

Regola: `cli/` e `mcp/` non contengono logica di business, non toccano Catalog, parser o LightRAG.

### Entry point e import
- `bin/ragcli` resta l'unico launcher e chiama il main di `cli`. Tutti gli altri `bin/*.py` vengono eliminati.
- `src/` va sul `sys.path` in un punto solo per contesto: il launcher, la configurazione pytest (`pythonpath`), l'avvio del server MCP. Le manipolazioni sparse di `sys.path` spariscono.
- ROOT, HOME, KB_ROOT, CONFIG si risolvono in una sola funzione di `tools/system`.

### Consolidamento dei duplicati
Per ogni gruppo si sceglie la copia più completa come base, si portano dentro le funzioni mancanti dalle altre, poi si cancellano le altre:

- registro KB: `kb_registry` / `kb_manager` / `kb_config` → `tools/kb`;
- memoria: `memory_manager` / `mcp/memories` → `tools/memory`;
- ingestion: `ingest` / `ingest_pipeline` / `consume_inbox` → `tools/ingest` + `pipeline`;
- router MCP: `router` / `kb_router` → `mcp/`;
- estrattore ZIP: quello robusto di `bin/archive_importer` confrontato con `ingest_pipeline.safe_extract_archive`; si tiene l'unione delle protezioni.

### Catalog e sorgenti
- Il Catalog per KB è l'autorità su sorgenti, file, documenti e job. Chiave file `(source_id, relative_path)`.
- Tipi di sorgente: `primary`, `folder`, `inbox` (una sola sorgente logica per KB, percorsi relativi per batch).
- `registry.yaml` conserva solo la configurazione a livello di KB (nome, porta, directory, runtime). Le sorgenti esterne migrano nel Catalog.
- Spariscono `origin_host` e `origin_folder`. Il Catalog dichiara una versione di schema esplicita.

### Scansione **[≠ GPT: aggiunta]**
- Se la sorgente è un work tree git, l'elenco dei file viene da `git ls-files --cached --others --exclude-standard`, che applica tutti i `.gitignore` annidati, `.git/info/exclude` e gli exclude globali, senza nuove dipendenze.
- Altrimenti c'è un walk senza seguire i symlink. In entrambi i casi poi si applicano i pattern di esclusione dell'utente.
- Snapshot, hash, diff e rename detection si spostano da `sync/client` nello scanner.
- Una scansione incompleta non autorizza mai cancellazioni. Restano le soglie di guardia esistenti (>20% o >100 file).

### Worker unico
- Ogni scansione o batch dell'inbox crea un job (id, idempotency key, KB, sorgente, stato, contatori, errori) eseguito dallo stesso worker.
- Restano i lock single-writer e il checkpoint di recovery. Spariscono gli stati remoti (es. TRANSFERRING).
- L'inbox mantiene ricezione, staging, coda e archivio dei processati, e non indicizza più da sola.

### Posizione dei dati della KB **[≠ GPT: aggiunta]**
- **Default:** i dati di una KB vivono dentro knowledge-os, in `kb/<kb-id>/` (Catalog, `derived/`, `rag_storage/`, `inbox/`, staging). Nessuna KB nuova scrive dentro le cartelle sorgente.
- **Switch:** un tool in `tools/kb` (`set_data_location`) sposta i dati tra `repo` (dentro knowledge-os) e `source` (dentro una cartella sorgente, in `<sorgente>/.lightrag`). Con più sorgenti, la destinazione si sceglie con l'argomento `source` (id o percorso); senza argomento e con più sorgenti il tool rifiuta e chiede quale.
- Lo spostamento è sicuro: richiede il lock di scrittura della KB e il server fermo, supporta `--dry-run`, copia e verifica prima di sostituire, aggiorna `registry.yaml` solo a copia riuscita e non cancella la vecchia posizione finché la nuova non è verificata.
- Quando i dati stanno dentro una sorgente, lo scanner li esclude sempre (`internal_excludes` dell'orchestrator).
- CLI e MCP espongono lo stesso tool (`ragcli kb data-location`), come ogni altra operazione.

### Parsing
- Il dispatcher decide per estensione e contenuto: text/md/code, JSON, JSONL, archivio ZIP, PDF, Office/EPUB/email, immagini, audio/video.
- JSON: validazione, riconoscimento della struttura (la detection esistente viene davvero usata), resa Markdown strutturata completa, metadati nel frontmatter. Il derivato va nella cache della KB, identificato da hash del contenuto + versione del parser.
- ZIP: estrazione in staging, ricorsione nel dispatcher con limite di profondità, provenienza `a.zip!/b.json`.
- Le preferenze trovate negli archivi non diventano memorie attive (fuori scope, vedi sotto).

### Rimozioni
- Downloader URL (moduli, comandi `import-url` e `retrieve url`, tool MCP `ingest_url`, test, dipendenza yt-dlp se non usata altrove). Whisper e il parsing locale di audio/video restano.
- `sync/server`, protocollo remoto, comando `sync`.
- Flag `link.consume`/`link.ingest` e comandi `link`.
- `ragcli job` diventa `ragcli ingest jobs`.

### CLI
Albero come §7 del piano GPT. Scope di default come §7.1. Output `--json` globale. Nessun traceback per input mancanti: prompt di selezione se interattivo, errore chiaro altrimenti.

### MCP
- Un server multi-KB. Ogni tool è un wrapper di una funzione `tools`.
- Auth via `Authorization: Bearer` o `X-API-Key`. L'auth via query parameter viene rimossa.
- `mcp auth set` legge da prompt nascosto o stdin. `rotate` riavvia il servizio se necessario.

### Migrazione dati
Comando idempotente con `--dry-run`:

- backup di Catalog e manifest;
- import delle sorgenti dal registry con id stabili;
- riconciliazione per checksum con i documenti già indicizzati (niente re-embedding);
- preservazione degli originali dell'inbox;
- nessuna cancellazione e report degli elementi non riconciliati.

## Testing Decisions

- Seam principale: le funzioni di `tools/`. I test chiamano `tools.*` con una KB temporanea e verificano il comportamento esterno: file nel Catalog, derivati prodotti, job creati. Non verificano dettagli interni.
- Seam secondari, pochi test di parità: CLI via subprocess su `bin/ragcli --json`, e MCP chiamando i tool del server FastMCP in-process. Verificano solo che l'adapter produca lo stesso risultato della funzione `tools`.
- LightRAG viene sostituito da un fake a livello di indexer, come fanno già i test esistenti.
- Test architetturale: analisi degli import di `cli/` e `mcp/`.
- Test di sicurezza ZIP: zip slip, symlink, path assoluto, bomba (ratio), profondità annidata, limiti di dimensione.
- Test `.gitignore`: repo temporaneo con `.gitignore` annidato e un `.env` ignorato.
- Prior art: la suite attuale in `tests/` (191 test verdi alla baseline `pre-reorg`). I test esistenti vengono riportati sui nuovi moduli, non riscritti da zero.
- Accettazione end-to-end (manuale, guidata): KB con `~/trading-agent` e `~/trading-agent-wiki`, creata una volta via CLI e una via MCP. Test dell'inbox con gli ZIP forniti dall'utente.

## Out of Scope

- Dashboard interattiva e autocomplete Bash/Zsh/Fish (solo riepilogo testuale non interattivo).
- Memory candidates estratti dagli archivi.
- Localizzazione dei messaggi.
- Nuovi formati oltre a quelli già supportati più JSON, JSONL e ZIP.

## Further Notes

- `AGENTS.md` va aggiornato con la regola di layout (`cli/` e `mcp/` solo via `tools/`), la regola `.gitignore`, e un vincolo per i subagenti: lavorare solo dentro il repo, nessun accesso fuori dalla directory del progetto. Nel `GLOSSARY.md`, "Staged Sync" diventa "Source Scan" e "MCP Router" diventa "MCP Server".
- Ordine obbligato: prima consolidamento ed entry point (verde a ogni passo), poi Catalog e scanner, poi worker unico, poi parsing, poi rimozioni, poi CLI/MCP finali, infine migrazione. Nessuna rimozione prima che il nuovo percorso sia verde.

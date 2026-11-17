# Ticket — centralize-tools

Spec: `docs/centralize-tools/spec.md`. Ogni ticket è autonomo. Dipendenze tra parentesi.

## Fase A — parallelizzabili (file distinti, nessuna cancellazione condivisa)

- ✅ **T1 · Un solo albero di codice.** Test e moduli importano da `src/` (`cli.*`), non da `bin.*`. Un solo punto di `sys.path` (pytest `pythonpath`, launcher). Ripristinare in `src/cli/archive_importer.py` l'`import zipfile`. Eliminare `bin/*.py` duplicati SOLO dopo che i test passano. `bin/ragcli` resta unico launcher. Gate: `.venv/bin/pytest -q` verde.
- ✅ **T2 · Scanner git-aware.** Nuovo `src/pipeline/source_scanner.py`: elenco file di una sorgente (git ls-files se work tree git, altrimenti walk senza symlink), poi esclusioni utente. Nuovo `tests/test_source_scanner.py` (`.gitignore` annidato, `.env` ignorato, symlink non seguiti). Non modifica altri file.
- ✅ **T3 · Estrattore ZIP sicuro.** Nuovo `src/pipeline/archive_parser.py`: unione delle protezioni di `bin/archive_importer.py` e `src/cli/ingest_pipeline.py:safe_extract_archive` (zip slip, symlink, path assoluti, limiti file/totale, ratio, profondità, provenienza `a.zip!/b`). Nuovo `tests/test_archive_parser.py`. Non modifica altri file.

## Fase B — dopo A

- ✅ **T4 · Catalog come autorità sulle sorgenti** (T1). Tipi primary/folder/inbox, schema versionato, migrazione sorgenti da `registry.yaml`.
- ✅ **T5 · Worker unico + orchestrator** (T2, T4). Scanner → job → worker → Catalog. Inbox diventa coda di intake. Elimina `consume_inbox`.
- ✅ **T6 · Parser JSON/JSONL strutturale e dispatcher** (T3, T5). `pipeline/json_parser.py` (detection strutturale, Markdown deterministico, JSONL per record, JSON invalido → fallback testo), `pipeline/expansion.py` (ZIP in staging con provenienza `a.zip!/inner`), integrati nell'orchestrator per sorgenti cartella e inbox; `ingest_json_transform` rimosso.

## Fase C — dopo B

- **T7 · `tools/` unico** (T5). Un solo registro KB e una sola memoria. Include `tools/kb.set_data_location` (default dati in `kb/<kb-id>/` dentro knowledge-os; switch verso/da una cartella sorgente scelta con l'argomento `source`; dry-run, lock di scrittura, copia verificata prima di sostituire) e il default dei nuovi dati in `kb/<kb-id>/` al posto di `<sorgente>/.lightrag`.
- **T8 · Adapter CLI e MCP** (T7). Albero comandi (incluso `kb data-location`), un solo server MCP, `mcp auth`, test architetturale degli import.
- **T9 · Rimozioni** (T8). URL importer, sync server/protocollo, link.
- **T10 · Migrazione dati con dry-run** (T9). Poi test end-to-end sulla KB trading e sull'inbox con gli ZIP dell'utente.

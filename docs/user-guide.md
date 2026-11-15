# Guida Rapida per l'Utente

Benvenuto in **KnowledgeOS**! Questa guida spiega come iniziare a usare il sistema in 3 semplici passi, senza competenze tecniche complesse.

---

## 1. Dove mettere i tuoi documenti?

Trascina semplicemente i tuoi file (appunti `.md`, documenti `.pdf`, fogli di calcolo `.xlsx`, immagini o registrazioni audio) all'interno della cartella:

```text
knowledge/default/
```

Il sistema è pronto a ricevere qualsiasi formato comune e a organizzarne la conoscenza semantica.

---

## 2. Come avviare e indicizzare?

Dalla cartella principale del progetto puoi usare il comando unico `./ragcli`:

### A. Controlla che tutto sia a posto
```bash
./ragcli doctor
```
Verifica che i motori di intelligenza artificiale locali (Ollama / LightRAG) siano attivi e funzionanti.

### B. Indicizza i tuoi documenti
```bash
./ragcli ingest default
```
Elabora i nuovi file aggiunti nella cartella `knowledge/default/` estraendo tabelle, testo e vettori.

### C. Visualizza lo stato delle tue basi di conoscenza
```bash
./ragcli list
```

---

## 3. Connetti il tuo assistente AI (Claude Code, Cursor)

Per permettere a Claude Code o al tuo agente di accedere alla conoscenza locale, esegui:

```bash
./ragcli mcp add default --client claude-code
```

L'assistente potrà ora consultare i tuoi documenti e la memoria a lungo termine in modo trasparente!

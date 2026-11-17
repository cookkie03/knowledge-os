# Handoff: OpenAI-compatible endpoints for every model call

## Goal

Ollama on this host runs on CPU only: one document takes tens of minutes, so a real KB (about 150 to 700 files) cannot be indexed. Every model call in the project must be able to use any OpenAI-compatible endpoint (OmniRoute, OpenRouter, vLLM, LM Studio, a remote Ollama `/v1`), chosen per role, with Ollama kept as one option and not as the default assumption.

## What exists today

- `src/tools/kb.py:write_kb_env` builds each KB's `.lightrag/.env`. It knows two providers only, `ollama` and `openrouter`, and the provider switches LLM and embedding together (`LLM_PROVIDER` in `config/global.env`).
- Roles that call a model: (1) LLM for entity and relation extraction and for answers, (2) embeddings, (3) rerank (currently `RERANK_BINDING=null`), (4) OCR and image description (`OCR_BACKEND`: docling, mineru, glmocr via Ollama), (5) audio transcription (Whisper, local only), (6) the dreaming/memory consolidation cycle (`ragcli memory dream`). Check each of these call sites; only (1) and (2) are wired to `write_kb_env`.
- The KB `.env` is read by `lightrag-server@<kb>.service`; `ragcli start/stop` and `merged_runtime_env` pass it to workers.

## Required design

1. Per-role settings, each with `binding` (`ollama` | `openai`), `host`, `model`, `api_key_env`: roles `llm`, `embedding`, `rerank`, `vision`, `transcription`, `dreaming`. Per-KB override of the global default.
2. API keys are never written into tracked files. Store only the name of an environment variable or a path to a secrets file (mode 600), and resolve it when the KB `.env` is generated. `kb/` is git-ignored but `config/global.env.example` is tracked.
3. Embedding dimension guard: the dimension is fixed per KB. Store `embedding_model` and `embedding_dim` in the KB config and refuse to start or ingest when they differ from the vector store, with a message explaining that a rebuild is needed. Never mix two embedding models in one KB.
4. `ragcli config` and `ragcli doctor` must show the effective endpoint per role and probe it (a one-token chat call, a one-text embedding call, the returned embedding dimension).
5. Rate limits and failures: retry with backoff on 429 and 5xx, and report a clear error when credits are exhausted instead of marking the document failed forever (see failed documents below).
6. Ingestion throughput: `pipeline/indexer.py` submits one document and waits for PROCESSED before the next (`wait_for_processed`), so `MAX_ASYNC` is never used. With a remote endpoint submit documents in small batches (configurable, default 4) and poll them together.

## Facts found while testing OmniRoute (2026-11-17)

- Local proxy: `http://127.0.0.1:20128/v1` (tailnet name `omniroute.tail0f7f67.ts.net`, the request mentions `tail0f6f67`, check which is right). The API key is in `~/.omniroute/.env`; the file holds two `OMNIROUTE_API_KEY` lines and only the second one is accepted by `/v1/models`. Do not copy the key into the repository.
- Chat combo `free` works (`model: "free"`, answer comes with a `reasoning` field; the content is in `content`). Suitable for extraction.
- Embedding combo `embedding` is broken on the OmniRoute side: it mixes models with dimensions 2560 and 4096 and OmniRoute refuses it ("incompatible vector dimensions"). It does not contain Qwen3-Embedding-0.6B (1024 dimensions) as expected. Fix the combo in OmniRoute first.
- Direct embedding routes tested: `openrouter/qwen/qwen3-embedding-8b`, `siliconflow/Qwen/Qwen3-Embedding-0.6B`, `deepinfra/Qwen/Qwen3-Embedding-0.6B` all failed for missing credit or balance.
- Until a working embedding route exists, the practical setup is mixed: LLM on OmniRoute `free`, embeddings on local Ollama `qwen3-embedding:0.6b` (fast enough on CPU, 1024 dimensions).

## Also fix while there

- A document that fails because of a transient provider error must be retried by the next pass; today the Catalog and LightRAG keep it as failed.
- LightRAG thinking models: `OLLAMA_LLM_THINK=false` is required for Ollama thinking models, otherwise reasoning consumes `NUM_PREDICT` and extraction returns empty content.

## Tests

Unit tests with a fake OpenAI-compatible HTTP server (stdlib `http.server`): per-role config rendering, secrets never in the generated tracked files, dimension guard, probe in `doctor`, retry on 429. One manual E2E on a small KB against OmniRoute once the embedding combo is fixed.

## Constraints

Follow `AGENTS.md`: simple procedural Python, atomic writes, path validation, English comments. Do not touch `~/trading-agent/brokers` or any `.env` file. Another session is editing the same checkout (ingest resilience); commit only your own files by explicit path.

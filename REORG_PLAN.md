# Reorganization Plan: Separate CLI and MCP, Centralize Ingestion Pipeline

## Overview

This plan reorganizes the `knowledge-os` codebase to:
1. Separate CLI tools from MCP servers at the file level
2. Centralize the ingestion pipeline in a single file
3. Centralize KB and Tull configuration
4. Centralize hook management
5. Move memory management into a single MCP file

## Current Structure Analysis

### CLI (`src/cli/`):
- `agent_hooks.py` - Agent hook management (pre-prompt context, post-session capture, client hook installation)
- `archive_importer.py` - Archive import with ZIP-Slip protection, normalization to Markdown
- `config_manager.py` - Global environment config management (`global.env`)
- `ingest_manager.py` - Ingestion pipeline orchestration (runs ingest.py script)
- `kb_manager.py` - KB creation and management
- `memory_manager.py` - Memory management (remember, recall, capture)
- `mcp_clients.py` - MCP server registration for various clients (claude-code, claude-desktop, codex, etc.)
- `ragcli` - CLI entry point script

### MCP (`src/mcp/`):
- `kb_router.py` - Multi-KB MCP router with LightRAG integration
- `lightrag_mcp.py` - LightRAG MCP server with tools (ask, retrieve_context, find_documents)
- `__init__.py` - Empty

### Pipeline (`src/pipeline/`):
- `indexer.py` - Document indexing and pipeline orchestration
- `parsers.py` - File type extensions and parsing logic
- `dreaming.py` - Memory management and dreaming (sleep consolidation)

### Storage (`src/storage/`):
- `catalog.py` - Source, file, document, job management
- `inbox.py` - Inbox queue and lifecycle manager
- `json_store.py` - Persistent catalog snapshots with locking
- `storage_manager.py` - JSON store operations

### Other:
- `config/global.env` - Global runtime configuration
- `config/registry.yaml` - KB registry
- `kb/` - Knowledge base storage root
- `src/cli/archive_importer.py` - Archive import (duplicate of pipeline concern)

## Reorganization Goals

### 1. Separate CLI and MCP
- Move MCP-related CLI commands out of `src/cli/mcp_clients.py` into proper MCP server files
- Create dedicated MCP server entry points
- Keep CLI-specific tools in `src/cli/`

### 2. Centralize Ingestion Pipeline
- Create a unified `ingest.py` that all modules can import
- Move pipeline logic from `src/cli/ingest_manager.py` into the central pipeline
- Ensure all consumers use the same pipeline

### 3. Centralize KB and Tull Configuration
- Move KB creation, configuration, and Tull management to a single location
- Create `kb_config.py` or similar for KB setup
- Centralize move/remove/add list operations

### 4. Centralize Hook Management
- Move hook installation and management to `src/mcp/` or a dedicated hooks module
- Centralize UserPromptSubmit and SessionEnd hook configuration

### 5. Move Memory into Single MCP File
- Consolidate memory management from `src/pipeline/dreaming.py` and `src/cli/memory_manager.py`
- Create single `mcp/memories.py` or integrate into `kb_router.py`
- Keep core memory types but unify the API

## Execution Phases

### Phase 1: Create Central Ingestion Pipeline
- Create `/home/luca/knowledge-os/src/cli/ingest_pipeline.py` - unified ingestion entry point
- Update `ingest_manager.py` to use the new centralized pipeline
- Update `archive_importer.py` to use centralized parsing

### Phase 2: Centralize KB Configuration
- Create `/home/luca/knowledge-os/src/cli/kb_config.py` - KB creation and configuration
- Move KB entry creation logic from `kb_manager.py` and `storage_manager.py`
- Centralize Tull (move/remove/add) operations

### Phase 3: Centralize Hook Management
- Create `/home/luca/knowledge-os/src/mcp/hooks.py` - Hook management
- Move hook installation logic from `agent_hooks.py`
- Create dedicated MCP hook endpoints

### Phase 4: Consolidate Memory Management
- Create `/home/luca/knowledge-os/src/mcp/memories.py` - Unified memory management
- Consolidate from `src/pipeline/dreaming.py`, `src/cli/memory_manager.py`, `src/cli/agent_hooks.py`
- Keep all memory types (preference, decision, episodic, fact) but unify API

### Phase 5: Update All Imports and Consumers
- Update all files that import from reorganized modules
- Ensure backward compatibility where possible
- Run tests to verify everything works

## Files to Create

1. `src/cli/ingest_pipeline.py` - Central ingestion pipeline
2. `src/cli/kb_config.py` - KB configuration and management
3. `src/mcp/hooks.py` - Hook management for MCP
4. `src/mcp/memories.py` - Unified memory management

## Files to Modify

1. `src/cli/ingest_manager.py` - Use centralized pipeline
2. `src/cli/archive_importer.py` - Use centralized parsing
3. `src/cli/kb_manager.py` - Use kb_config
4. `src/cli/memory_manager.py` - Use centralized memories
5. `src/mcp/kb_router.py` - Use centralized memories and hooks
6. `src/mcp/lightrag_mcp.py` - Use centralized components
7. `src/pipeline/dreaming.py` - Redirect to centralized memories
8. `src/mcp/__init__.py` - Export new modules

## Safety Requirements (Non-negotiable)

- All file writes must use `fcntl.flock` for locking, `tempfile.mkstemp` for temporary files, and `os.replace` for atomic replacement
- All paths must be resolved with `os.path.abspath` or `Path.resolve()` before reading/writing
- All critical blocks must have Redis-style comments explaining purpose and invariants

## Test Impact

All existing tests should continue to pass. Key test areas:
- `test_ingest*.py` - Ingestion pipeline tests
- `test_mcp*.py` - MCP server tests
- `test_dreaming.py` - Memory dreaming tests
- `test_agent_hooks.py` - Agent hook tests
- `test_catalog_json_contract.py` - Catalog contract tests
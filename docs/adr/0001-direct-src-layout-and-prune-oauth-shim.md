# 1. Direct src/ Layout and Prune OAuth Shim

Date: 2026-11-16
Status: Accepted

## Context
`knowledge-os` accumulated 11 top-level folders, over 1,500 lines of duplicated logic across ingestion and parsing, legacy macOS launchd scripts, and a 620-line OAuth 2.1 authorization server (`services/obsidian-mcp-oauth-shim`) only intended for exposing web connectors to `claude.ai`.
Non-technical users found the repository tree opaque, with no clear place for user documents or obvious entry points.

## Decision
1. Remove `services/obsidian-mcp-oauth-shim` and legacy watchdog scripts to keep the core local-first and eliminate SQLite dependencies from the main tree.
2. Adopt a direct `src/` layout without an unnecessary intermediate nested folder (`src/__init__.py`, `src/cli/`, `src/pipeline/`, `src/storage/`, `src/mcp/`, `src/sync/`).
3. Relocate systemd unit templates to `deploy/systemd/`.
4. Provide a drop-in user document directory `knowledge/default/` and root CLI wrapper `./ragcli`.
5. Standardize packaging via `pyproject.toml` and pytest configuration via `pytest.ini`.

## Consequences
- Total lines of code reduced by over 1,200 LOC.
- Clean root directory with obvious user interfaces (`knowledge/`, `ragcli`, `config/`).
- 100% test suite compatibility preserved across all 191 regression tests.

# Domain Glossary

## Knowledge Base (KB)
An isolated domain container grouping source documents, extracted entities, relationship graphs, and vector embeddings under a unique name and port.

## Document Occurrence
A concrete record of a file present at a specific relative path within a source, tracked via its SHA-256 digest so file moves and renames require zero re-embedding.

## Catalog
The content-addressable JSON transaction manifest tracking active sources, document occurrences, reference counts, and sync jobs with POSIX file locks (`fcntl.flock`).

## Inbox
An append-only queue with guarded staging states (`pending/` -> `processing/` -> `processed/` or `failed/`) ensuring incoming files are never lost or destructively overwritten.

## Dreaming
An autonomous memory synthesis daemon that clusters raw episodic session learnings, resolves contradictory instructions, and deprecates stale context into long-term agent guidelines.

## Staged Sync
A non-destructive delta synchronization engine that scans folders without following symlinks and aborts with review gates if deletion thresholds (>20% or >100 files) are breached.

## MCP Router
Model Context Protocol service exposing multi-KB semantic search, evidence inspection, and episodic memory tools to AI agents (Claude Code, Cursor, Codex).

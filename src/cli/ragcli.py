#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import json
import shutil
import subprocess
import sys
from typing import Any
import time
from pathlib import Path

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - exercised via monkeypatch in tests
    requests = None

from tools.agents import clients as mcp_clients
from tools.kb import add_kb, set_data_location, canonicalize_exclude_specs, ensure_kb_dirs, expand_exclude_specs, find_kb, load_registry, merged_runtime_env, normalize_exclude_specs_for_kb, split_exclude_specs, save_registry, storage_mode, write_kb_env
from tools.system import HOME, MCP_SCRIPT, KB_ROOT, global_env, upsert_global_env
from tools.archive import import_archive_to_kb
from tools.agents.hooks import (
    capture_post_session,
    inject_pre_prompt_context,
    install_client_hook,
)
from tools.memory import (
    deprecate_memory_by_id,
    load_kb_memories,
    query_memories,
    remember_memory,
    run_dreaming_cycle,
)
from tools import ingest as ingest_tools
from tools import services
from tools.retrieval import Retrieval

def cmd_doctor(args: argparse.Namespace) -> None:
    report = services.doctor_report()
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        for item in report:
            print(f"{item['name']}: {item['details']}")
    if not all(bool(item["ok"]) for item in report):
        raise SystemExit(1)


def cmd_service(args: argparse.Namespace) -> None:
    handlers = {
        "status": cmd_service_status,
        "restart": cmd_service_restart,
        "logs": cmd_service_logs,
    }
    handlers[args.service_cmd](args)


def cmd_service_status(args: argparse.Namespace) -> None:
    units = [services.normalize_service_name(args.name)] if getattr(args, "name", None) else services.status_linux_units(global_env())
    for unit in units:
        status = services.service_status(unit)
        print(f"{unit}: {status['details']}")


def cmd_service_restart(args: argparse.Namespace) -> None:
    units = [services.normalize_service_name(args.name)] if getattr(args, "name", None) else services.manageable_linux_units(global_env())
    for unit in units:
        result = services.systemctl_user("restart", unit)
        if result.returncode != 0:
            raise SystemExit(result.stderr.strip() or f"restart failed: {unit}")
        print(f"restarted {unit}")


def cmd_service_logs(args: argparse.Namespace) -> None:
    units = [services.normalize_service_name(args.name)] if getattr(args, "name", None) else services.manageable_linux_units(global_env())
    for unit in units:
        result = services.journalctl_user("-n", "100", "--no-pager", "-u", unit)
        if len(units) > 1:
            print(f"== {unit} ==")
        output = result.stdout.strip() or result.stderr.strip()
        if output:
            print(output)
        if result.returncode != 0:
            raise SystemExit(result.returncode)


def cmd_service_install_units(_: argparse.Namespace) -> None:
    installed = services.install_user_units(HOME, Path.home(), global_env())
    print(f"installed user units: {', '.join(path.name for path in installed)}")


def port_pid(port: int) -> str | None:
    result = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True)
    return result.stdout.strip() or None


def health(port: int) -> bool:
    if requests is None:
        return False
    try:
        return requests.get(f"http://127.0.0.1:{port}/health", timeout=2).ok
    except Exception:
        return False


def _ensure_runtime(env: dict[str, str]) -> tuple[Path, Path]:
    engine_python = Path(env["ENGINE_PYTHON"]).expanduser()
    server_bin = Path(env["LIGHTRAG_SERVER_BIN"]).expanduser()
    missing = [str(p) for p in (engine_python, server_bin) if not p.exists()]
    if missing:
        sys.exit(
            "Shared runtime is not ready:\n"
            + "\n".join(f"  - missing {item}" for item in missing)
            + "\nRun `./setup.sh` to install LightRAG + Docling in the shared runtime."
        )
    return engine_python, server_bin


def _wait_for_health(port: int, timeout: float = 30.0) -> bool:
    started = time.time()
    while time.time() - started < timeout:
        if health(port):
            return True
        time.sleep(0.5)
    return False


def _wait_for_port_release(port: int, timeout: float = 10.0) -> bool:
    started = time.time()
    while time.time() - started < timeout:
        if not port_pid(port):
            return True
        time.sleep(0.5)
    return False


def _stop_server(kb: dict) -> bool:
    pid = port_pid(kb["port"])
    if not pid:
        return True
    for part in pid.split():
        subprocess.run(["kill", part], capture_output=True, text=True)
    if _wait_for_port_release(kb["port"]):
        return True
    pid = port_pid(kb["port"])
    if not pid:
        return True
    for part in pid.split():
        subprocess.run(["kill", "-9", part], capture_output=True, text=True)
    return _wait_for_port_release(kb["port"], timeout=3.0)


def _kb_server_state(kb: dict) -> str:
    if health(kb["port"]):
        return "up"
    if port_pid(kb["port"]):
        return "zombie"
    return "down"


# Attempt every client removal and collect failures instead of stopping early.
def _deregister_all_mcp(kb: dict) -> list[str]:
    mcp_name = f"lightrag-{kb['name']}"
    warnings: list[str] = []
    for client, fn in mcp_clients.REMOVERS.items():
        try:
            rc = fn(mcp_name, Path(kb["source_dir"]))
        except Exception as exc:
            warnings.append(f"{client}: {exc}")
            continue
        if rc != 0:
            warnings.append(f"{client}: exit code {rc}")
    return warnings


# Permit deletion only for the two recognized KB data directory locations.
def _safe_kb_delete_target(kb: dict) -> Path:
    data_dir = Path(kb["data_dir"]).resolve()
    source_data_dir = (Path(kb["source_dir"]).resolve() / ".lightrag").resolve()
    repo_data_dir = (KB_ROOT / kb["name"]).resolve()
    if data_dir == source_data_dir or data_dir == repo_data_dir:
        return data_dir
    sys.exit(
        f"Safety refusal: unexpected data_dir for '{kb['name']}': {data_dir}\n"
        f"Only these paths are allowed:\n"
        f"  - {source_data_dir}\n"
        f"  - {repo_data_dir}"
    )


def cmd_add(args: argparse.Namespace) -> None:
    # Thin adapter: creation, validation and the default data location (kb/<name>/
    # inside knowledge-os) live in tools.kb.add_kb.
    try:
        kb = add_kb(args.name, args.source_dir, port=args.port)
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"KB '{args.name}' created.")
    print(f"  source     : {kb['source_dir']}")
    print(f"  data dir   : {kb['data_dir']}")
    print(f"  rag_storage: {kb['rag_storage']}")
    print(f"  derived    : {kb['derived_dir']}")
    print(f"  porta      : {kb['port']}")
    print(f"\nProssimi passi:\n  ragcli start {args.name}\n  ragcli ingest {args.name}\n  ragcli mcp add {args.name}")


# Text summary shown by `ragcli` without arguments: MCP service state, then each KB
# with its server state and its most recent ingestion job.
def cmd_status(_: argparse.Namespace) -> None:
    mcp_state = services.service_status("lightrag-mcp.service")
    print(f"MCP service: {mcp_state['details']}")
    cmd_list(_)
    for kb in load_registry(global_env())["kbs"]:
        try:
            jobs = ingest_tools.list_jobs(kb["name"], limit=1)["jobs"]
        except (ValueError, RuntimeError):
            jobs = []
        if jobs:
            print(f"last job {kb['name']}: {jobs[0]['job_id']} {jobs[0]['stage']}")


def cmd_list(_: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    if not registry["kbs"]:
        print("No KBs. Create one with: ragcli add <name> <source_dir>")
        return
    print(f"{'NAME':<22}{'PORT':<7}{'MODE':<8}{'SERVER':<8}{'EXCL':<6}SOURCE")
    for kb in registry["kbs"]:
        print(
            f"{kb['name']:<22}{kb['port']:<7}{storage_mode(kb):<8}{_kb_server_state(kb):<8}"
            f"{len(kb.get('exclude', [])):<6}{kb['source_dir']}"
        )


def cmd_exclude_add(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' not found.")
    _canonicalize_kb_excludes(kb)
    patterns = kb.setdefault("exclude", [])
    added: list[str] = []
    input_specs, _ = canonicalize_exclude_specs(split_exclude_specs(args.pattern))
    for spec in input_specs:
        if spec in patterns or spec in added:
            continue
        patterns.append(spec)
        added.append(spec)
    _canonicalize_kb_excludes(kb)
    save_registry(registry)
    write_kb_env(kb)
    if added:
        display, _ = canonicalize_exclude_specs(added)
        print(f"Esclusi da '{args.name}': {', '.join(display)}")
    else:
        print(f"No new patterns to add for '{args.name}'.")


def cmd_exclude_rm(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' not found.")
    _canonicalize_kb_excludes(kb)
    patterns = kb.get("exclude", [])
    removed: list[str] = []
    targets, _ = canonicalize_exclude_specs(split_exclude_specs(args.pattern))
    for spec in targets:
        if spec not in patterns:
            continue
        patterns.remove(spec)
        removed.append(spec)
    if not patterns:
        kb["exclude"] = []
    save_registry(registry)
    write_kb_env(kb)
    if removed:
        print(f"Rimosse esclusioni da '{args.name}': {', '.join(removed)}")
    else:
        sys.exit(f"No patterns removed from '{args.name}'.")


def cmd_exclude_ls(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' not found.")
    changed = _canonicalize_kb_excludes(kb)
    if changed:
        save_registry(registry)
    source_dir = Path(kb["source_dir"])
    data_dir = Path(kb["data_dir"])
    patterns = kb.get("exclude", [])
    if not patterns:
        print(f"No exclusions defined for '{args.name}'.")
        return

    display_patterns = expand_exclude_specs(patterns, source_dir, data_dir) if args.expanded else patterns
    title = "Expanded exclusions" if args.expanded else "Exclusions"
    print(f"{title} for '{args.name}':")
    for pattern in display_patterns:
        print(f"  - {pattern}")


def _canonicalize_kb_excludes(kb: dict) -> bool:
    canonical, changed = normalize_exclude_specs_for_kb(
        kb.get("exclude", []),
        Path(kb["source_dir"]),
    )
    kb["exclude"] = canonical
    return changed


def cmd_start(args: argparse.Namespace) -> None:
    env = global_env()
    registry = load_registry(env)
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' not found.")
    ensure_kb_dirs(kb)
    write_kb_env(kb, env)
    _, server_bin = _ensure_runtime(env)
    state = _kb_server_state(kb)
    if state == "up":
        print(f"'{args.name}' already running su :{kb['port']}")
        return
    if state == "zombie":
        sys.exit(
            f"Server '{args.name}' not healthy ma porta :{kb['port']} occupata. "
            f"Run `ragcli stop {args.name}` e poi rilancia `ragcli start {args.name}`."
        )
    log_path = Path(kb["data_dir"]) / "lightrag.log"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("ab") as log_handle:
        log_handle.write(
            f"[{now_str}] === Starting LightRAG server for '{kb['name']}' on port {kb['port']} ===\n".encode("utf-8")
        )
        log_handle.flush()
        subprocess.Popen(
            [
                str(server_bin),
                "--host",
                "127.0.0.1",
                "--port",
                str(kb["port"]),
                "--input-dir",
                kb["source_dir"],
                "--working-dir",
                kb["rag_storage"],
            ],
            cwd=kb["data_dir"],
            env=merged_runtime_env(kb, env),
            stdout=log_handle,
            stderr=log_handle,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    if not _wait_for_health(kb["port"]):
        sys.exit(
            f"Il server di '{args.name}' did not become healthy. "
            f"Controlla il log in {log_path}"
        )
    print(f"Server '{args.name}' attivo su http://127.0.0.1:{kb['port']}")


def cmd_stop(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' not found.")
    state = _kb_server_state(kb)
    if state == "down":
        print(f"'{args.name}' already stopped.")
        return
    if not _stop_server(kb):
        sys.exit(
            f"Impossibile fermare '{args.name}' su :{kb['port']}. "
            f"Controlla manualmente il processo {port_pid(kb['port']) or '?'}.)"
        )
    log_path = Path(kb["data_dir"]) / "lightrag.log"
    if log_path.exists():
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with log_path.open("ab") as log_handle:
                log_handle.write(
                    f"[{now_str}] === Stopped LightRAG server for '{kb['name']}' ===\n".encode("utf-8")
                )
                log_handle.flush()
        except OSError:
            pass
    print(f"Server '{args.name}' stopped.")


def cmd_remove(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' not found.")
    state = _kb_server_state(kb)
    if state != "down":
        sys.exit(
            f"Stop the server for '{args.name}' first with `ragcli stop {args.name}`."
        )
    delete_target = _safe_kb_delete_target(kb)
    mcp_warnings = _deregister_all_mcp(kb)
    registry["kbs"] = [entry for entry in registry["kbs"] if entry["name"] != args.name]
    save_registry(registry)
    if delete_target.exists():
        shutil.rmtree(delete_target)
    print(f"KB '{args.name}' removed.")
    print(f"  source preserved: {kb['source_dir']}")
    print(f"  deleted state   : {delete_target}")
    if mcp_warnings:
        print("\nWarnings during MCP cleanup:")
        for warning in mcp_warnings:
            print(f"  - {warning}")


# Print a result either as JSON (--json) or through a text renderer.
def _emit(args: argparse.Namespace, data: dict, render=None) -> None:
    if getattr(args, "json", False) or render is None:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        return
    render(data)


# Start (default) or run in the foreground (--wait) one ingestion pass: folder
# sources plus inbox, through the single worker in tools.ingest.
def cmd_ingest(args: argparse.Namespace) -> None:
    if not getattr(args, "wait", False):
        started = ingest_tools.start_background(args.name)
        _emit(args, started, lambda d: print(f"Ingestion started for '{d['kb']}' (PID {d['pid']}). Log: {d['log']}"))
        return
    report = ingest_tools.run(args.name, source_ids=args.source or None,
                              include_inbox=not args.no_inbox, lock_timeout=300.0)
    _emit(args, report)
    if report["status"] != "ok":
        raise SystemExit(1)


# Queue a file or directory in the inbox and, unless --no-start is given, start the worker.
def cmd_inbox_send(args: argparse.Namespace) -> None:
    job = ingest_tools.inbox_add(args.name, path=str(Path(args.path).expanduser().resolve()))
    result = {"job": job}
    if not args.no_start:
        result["worker"] = ingest_tools.start_background(args.name)
    _emit(args, result, lambda d: print(f"Queued {d['job'].get('file_count', 1)} file(s) in job {d['job']['job_id']}."))


# Start the single worker so pending inbox batches are processed.
def cmd_inbox_consume(args: argparse.Namespace) -> None:
    started = ingest_tools.start_background(args.name)
    _emit(args, started, lambda d: print(f"Worker started for '{d['kb']}' (PID {d['pid']}). Log: {d['log']}"))


# Show inbox queue counts.
def cmd_inbox_status(args: argparse.Namespace) -> None:
    def render(status: dict) -> None:
        print(f"Inbox '{args.name}': pending {status['pending']}, processing {status['processing']}, "
              f"processed {status['processed']}, failed {status['failed']} (total {status['total']})")
    _emit(args, ingest_tools.inbox_status(args.name), render)


# Register an extra source folder of the KB in the Catalog.
def cmd_source_add(args: argparse.Namespace) -> None:
    source = ingest_tools.add_source(args.name, args.path, name=getattr(args, "label", None))
    _emit(args, source, lambda d: print(f"Source '{d['id']}' added: {d['path']}"))


# Detach a source from the KB by its id.
def cmd_source_rm(args: argparse.Namespace) -> None:
    result = ingest_tools.remove_source(args.name, args.source_id)
    _emit(args, result, lambda d: print(f"Source '{d['removed']}' removed ({d['files_detached']} files detached)."))


# List the sources of a KB with their active file counts.
def cmd_source_ls(args: argparse.Namespace) -> None:
    def render(data: dict) -> None:
        for source in data["sources"]:
            print(f"{source['id']:<22}{source['type']:<9}{source['active_files']:>6}  {source.get('path') or ''}")
    _emit(args, ingest_tools.list_sources(args.name), render)


# Ingest only the given sources (ids), in the foreground.
def cmd_source_ingest(args: argparse.Namespace) -> None:
    report = ingest_tools.run(args.name, source_ids=args.source_ids, include_inbox=False, lock_timeout=300.0)
    _emit(args, report)
    if report["status"] != "ok":
        raise SystemExit(1)


# Background entry point used by detached workers; prints the report and fails on a non-ok status.
def cmd_worker_run(args: argparse.Namespace) -> None:
    report = ingest_tools.run(args.name, lock_timeout=300.0)
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    if report["status"] != "ok":
        raise SystemExit(f"Ingestion not run for '{args.name}': {report['status']}")


# Job commands: status, wait (poll until a final stage), list, retry.
FINAL_JOB_STAGES = {"COMPLETED", "FAILED_RETRYABLE", "FAILED_FINAL", "NEEDS_REVIEW"}


def cmd_job(args: argparse.Namespace) -> None:
    if args.job_cmd == "list":
        data = ingest_tools.list_jobs(args.kb, stage=args.stage, limit=args.limit)
        def render(d: dict) -> None:
            for job in d["jobs"]:
                print(f"{job['job_id']:<34}{job['stage']:<18}{job['source_id'] or '-'}")
        _emit(args, data, render)
        return
    if args.job_cmd == "retry":
        _emit(args, ingest_tools.retry_job(args.kb, args.job_id, lock_timeout=300.0))
        return
    job = ingest_tools.get_job(args.kb, args.job_id)
    while args.job_cmd == "wait" and job["stage"] not in FINAL_JOB_STAGES:
        time.sleep(1)
        job = ingest_tools.get_job(args.kb, args.job_id)
    ok = args.job_cmd != "wait" or job["stage"] == "COMPLETED"
    _emit(args, {"ok": ok, "job_id": job["job_id"], "kb": job["kb_id"], "source": job["source_id"],
                 "state": job["stage"], **job["counters"]})
    if not ok:
        raise SystemExit(1)


def cmd_move(args: argparse.Namespace) -> None:
    # Thin adapter over tools.kb.set_data_location: without --to the location is
    # toggled between knowledge-os and the source folder, as before.
    registry = load_registry(global_env())
    kb = find_kb(registry, args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' not found.")
    location = args.to or ("repo" if storage_mode(kb) == "source" else "source")
    try:
        result = set_data_location(args.name, location, source=args.source, dry_run=args.dry_run)
    except ValueError as exc:
        sys.exit(str(exc))
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    if not result["moved"]:
        label = "Dry run" if result["dry_run"] else "No move needed"
        print(f"{label}: data of '{args.name}' {result['from']} -> {result['to']}")
        return
    print(f"KB '{args.name}' data moved to {result['to']} ({result['catalog_paths_updated']} catalog paths updated).")
    for warning in result["warnings"]:
        print(f"Warning: {warning}")


# Name under which the single multi-KB MCP server is registered in agent clients.
MCP_SERVER_NAME = "knowledge-os"


# Register the single multi-KB MCP server in one or all supported clients.
# `name` only picks the project folder used by project-level client configs.
def cmd_mcp_add(args: argparse.Namespace) -> None:
    env = global_env()
    kb = find_kb(load_registry(env), args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' not found.")
    engine_python, _ = _ensure_runtime(env)
    clients = list(mcp_clients.CLIENTS) if args.client == "all" else [args.client]
    rc = 0
    for client in clients:
        if len(clients) > 1:
            print(f"\n== {client} ==")
        fn = mcp_clients.CLIENTS[client]
        if fn(MCP_SERVER_NAME, engine_python, MCP_SCRIPT, None, args.print_only, Path(kb["source_dir"])) != 0:
            rc = 1
    sys.exit(rc)


# Remove the single MCP server registration from one or all clients.
def cmd_mcp_rm(args: argparse.Namespace) -> None:
    kb = find_kb(load_registry(global_env()), args.name)
    if not kb:
        sys.exit(f"KB '{args.name}' not found.")
    clients = list(mcp_clients.REMOVERS) if args.client == "all" else [args.client]
    rc = 0
    for client in clients:
        if len(clients) > 1:
            print(f"\n== {client} ==")
        try:
            if mcp_clients.REMOVERS[client](MCP_SERVER_NAME, Path(kb["source_dir"])) != 0:
                rc = 1
        except Exception as exc:
            print(f"Errore durante la de-registrazione da {client}: {exc}")
            rc = 1
    sys.exit(rc)


def cmd_memory_remember(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    kb = find_kb(registry, args.kb)
    if not kb:
        sys.exit(f"KB '{args.kb}' not found.")
    mem = remember_memory(
        source_dir=kb["source_dir"],
        content=args.content,
        memory_type=args.type,
        title=args.title or "",
        tags=args.tags.split(",") if args.tags else [],
        supersedes_id=args.supersedes,
    )
    print(json.dumps({"ok": True, "memory": mem}, indent=2, ensure_ascii=False))


def _registry_kbs(registry: dict[str, Any]) -> list[dict[str, Any]]:
    return list(registry.get("kbs", []))


# Search memories across every KB by default, or only selected KBs when scoped.
def cmd_memory_recall(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    selected = _registry_kbs(registry)
    legacy_kb = getattr(args, "kb_legacy", None)
    raw_query = args.query if isinstance(args.query, str) else " ".join(args.query)
    query_parts = [raw_query]
    if legacy_kb and not raw_query:
        query_parts = [legacy_kb]
        legacy_kb = None
    selected_kbs = args.kb or ([legacy_kb] if legacy_kb else None)
    query = " ".join(query_parts)
    if selected_kbs:
        selected = [kb for kb in selected if kb.get("name") in selected_kbs]
    results: list[dict[str, Any]] = []
    for kb in selected:
        memories = query_memories(
            source_dir=kb["source_dir"],
            query=query,
            memory_type=args.type,
            active_only=not args.all,
            top_k=args.top_k,
        )
        for memory in memories:
            item = dict(memory)
            item["kb"] = kb["name"]
            results.append(item)
    results.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
    results = results[:args.top_k]
    print(json.dumps({"ok": True, "count": len(results), "memories": results}, indent=2, ensure_ascii=False))


# Retrieve generic information from selected KB backends using one hierarchical command.
def cmd_retrieve_ask(args: argparse.Namespace) -> None:
    router = Retrieval()
    selected = args.kb or [kb["name"] for kb in _registry_kbs(load_registry(global_env()))]
    responses = []
    for kb in selected:
        responses.append({"kb": kb, "response": router.ask(kb=kb, question=args.query, strategy=args.strategy, response_format=args.format)})
    print(json.dumps({"ok": True, "count": len(responses), "results": responses}, indent=2, ensure_ascii=False))


def cmd_memory_dream(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    selected = _registry_kbs(registry)
    if not args.all:
        kb = find_kb(registry, args.kb)
        if not kb:
            sys.exit(f"KB '{args.kb}' not found.")
        selected = [kb]
    reports = []
    for kb in selected:
        report = run_dreaming_cycle(source_dir=kb["source_dir"], dry_run=args.dry_run)
        reports.append({"kb": kb["name"], "report": report})
    print(json.dumps({"ok": True, "reports": reports}, indent=2, ensure_ascii=False))


def cmd_memory_list(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    kb = find_kb(registry, args.kb)
    if not kb:
        sys.exit(f"KB '{args.kb}' not found.")
    memories = load_kb_memories(kb["source_dir"])
    if args.type:
        memories = [m for m in memories if m["type"] == args.type]
    if args.status:
        memories = [m for m in memories if m["status"] == args.status]
    print(json.dumps({"ok": True, "count": len(memories), "memories": memories}, indent=2, ensure_ascii=False))


def cmd_memory_deprecate(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    kb = find_kb(registry, args.kb)
    if not kb:
        sys.exit(f"KB '{args.kb}' not found.")
    mem = deprecate_memory_by_id(
        source_dir=kb["source_dir"],
        memory_id=args.memory_id,
        reason=args.reason or "",
        superseded_by=args.superseded_by,
    )
    if not mem:
        sys.exit(f"Memoria '{args.memory_id}' not found.")
    print(json.dumps({"ok": True, "memory": mem}, indent=2, ensure_ascii=False))


def cmd_import_archive(args: argparse.Namespace) -> None:
    registry = load_registry(global_env())
    kb = find_kb(registry, args.kb)
    if not kb:
        sys.exit(f"KB '{args.kb}' not found.")
    res = import_archive_to_kb(
        archive_path=args.archive,
        output_source_dir=kb["source_dir"],
        provider=args.format or "auto",
    )
    print(json.dumps({"ok": True, "result": res}, indent=2, ensure_ascii=False))


def cmd_hook_inject(args: argparse.Namespace) -> None:
    prompt = args.prompt
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read()
    res = inject_pre_prompt_context(args.kb, prompt)
    if res:
        print(res)


def cmd_hook_capture(args: argparse.Namespace) -> None:
    text = args.text
    if not text and not args.transcript and not sys.stdin.isatty():
        text = sys.stdin.read()
    res = capture_post_session(
        kb_name=args.kb,
        transcript_path=args.transcript,
        raw_text=text,
        session_id=args.session_id,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))


def cmd_hook_install(args: argparse.Namespace) -> None:
    res = install_client_hook(
        client=args.client,
        kb_name=args.kb,
        target_project_dir=args.project_dir,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False))


def _format_on_off(val: str | None, default: str = "1") -> str:
    active = (val if val is not None else default).strip().lower() not in {"0", "false", "no", "off"}
    return "[ON]" if active else "[OFF]"


# Display central configuration panel organized by pipeline phases.
def cmd_config_show(_args: argparse.Namespace) -> None:
    env = global_env()
    print("=" * 70)
    print("                   LIGHTRAG-KB CONFIGURATION PANEL")
    print("=" * 70)
    print("\n[FASE 1: DISCOVERY & HASHING]")
    print("  Hash Algorithm:       SHA256 (rilevamento modifiche su sorgente)")
    print("  Storage Format:       JSON atomico protetto da flock (no SQLite/Neo4j)")
    print("  Derived Artifacts:    .lightrag/derived/ (cache markdown normalizzata)")

    print("\n[FASE 2: PARSING & EXTRACTION (DOCLING + NATIVE)]")
    print(f"  Pipeline Text/Code:   {_format_on_off(env.get('PIPELINE_ENABLE_TEXT')):<6} (PIPELINE_ENABLE_TEXT)")
    print(f"  Pipeline PDF:         {_format_on_off(env.get('PIPELINE_ENABLE_PDF')):<6} (PIPELINE_ENABLE_PDF)")
    print(f"  Pipeline Images:      {_format_on_off(env.get('PIPELINE_ENABLE_IMAGES')):<6} (PIPELINE_ENABLE_IMAGES)")
    print(f"  Pipeline Office/ODF:  {_format_on_off(env.get('PIPELINE_ENABLE_OFFICE')):<6} (PIPELINE_ENABLE_OFFICE)")
    print(f"  Pipeline Audio/Video: {_format_on_off(env.get('PIPELINE_ENABLE_AUDIO')):<6} (PIPELINE_ENABLE_AUDIO)")
    print(f"  ASR Engine:           {env.get('DOCLING_ASR_ENGINE', 'whisper_turbo_native')} (Whisper)")
    print(f"  OCR Engine:           {env.get('DOCLING_OCR_ENGINE', 'auto')} (Docling Linux: easyocr/rapidocr)")
    print(f"  Table Extraction:     {_format_on_off(env.get('DOCLING_ENABLE_TABLES')):<6} (DOCLING_ENABLE_TABLES)")
    print(f"  Code Enrichment:      {_format_on_off(env.get('DOCLING_ENABLE_CODE_ENRICHMENT')):<6} (DOCLING_ENABLE_CODE_ENRICHMENT)")
    print(f"  Formula Enrichment:   {_format_on_off(env.get('DOCLING_ENABLE_FORMULA_ENRICHMENT')):<6} (DOCLING_ENABLE_FORMULA_ENRICHMENT)")

    print("\n[FASE 3: CHUNKING & GRAPH EXTRACTION]")
    print(f"  Chunk Size:           {env.get('CHUNK_SIZE', '800')} tokens")
    print(f"  Chunk Overlap:        {env.get('CHUNK_OVERLAP_SIZE', '80')} tokens")
    print(f"  Max Gleaning:         {env.get('MAX_GLEANING', '1')} pass (estrazione entità/relazioni)")
    print(f"  Summary Language:     {env.get('SUMMARY_LANGUAGE', 'Italian, English')}")

    print("\n[FASE 4: EMBEDDING & LLM (OLLAMA)]")
    print(f"  Provider:             {env.get('LLM_PROVIDER', 'ollama')}")
    print(f"  Ollama Host:          {env.get('OLLAMA_HOST', 'http://localhost:11434')}")
    print(f"  LLM Model:            {env.get('LLM_MODEL', 'qwen3.5:4b')}")
    print(f"  Embedding Model:      {env.get('EMBEDDING_MODEL', 'embeddinggemma-2:latest')} (dim: {env.get('EMBEDDING_DIM', '768')})")
    print(f"  Max Async:            {env.get('MAX_ASYNC', '1')} (concorrenza LLM)")
    print(f"  LLM Timeout:          {env.get('LLM_TIMEOUT', '900')}s")
    print("=" * 70)


# Set configuration parameters atomically and update global.env.
def cmd_config_set(args: argparse.Namespace) -> None:
    updates: dict[str, str] = {}
    if getattr(args, "enable_text", False):
        updates["PIPELINE_ENABLE_TEXT"] = "1"
    if getattr(args, "disable_text", False):
        updates["PIPELINE_ENABLE_TEXT"] = "0"

    if getattr(args, "enable_pdf", False):
        updates["PIPELINE_ENABLE_PDF"] = "1"
    if getattr(args, "disable_pdf", False):
        updates["PIPELINE_ENABLE_PDF"] = "0"

    if getattr(args, "enable_images", False):
        updates["PIPELINE_ENABLE_IMAGES"] = "1"
    if getattr(args, "disable_images", False):
        updates["PIPELINE_ENABLE_IMAGES"] = "0"

    if getattr(args, "enable_office", False):
        updates["PIPELINE_ENABLE_OFFICE"] = "1"
    if getattr(args, "disable_office", False):
        updates["PIPELINE_ENABLE_OFFICE"] = "0"

    if getattr(args, "enable_audio", False):
        updates["PIPELINE_ENABLE_AUDIO"] = "1"
    if getattr(args, "disable_audio", False):
        updates["PIPELINE_ENABLE_AUDIO"] = "0"

    if getattr(args, "enable_tables", False):
        updates["DOCLING_ENABLE_TABLES"] = "1"
    if getattr(args, "disable_tables", False):
        updates["DOCLING_ENABLE_TABLES"] = "0"

    if getattr(args, "enable_code", False):
        updates["DOCLING_ENABLE_CODE_ENRICHMENT"] = "1"
    if getattr(args, "disable_code", False):
        updates["DOCLING_ENABLE_CODE_ENRICHMENT"] = "0"

    if getattr(args, "enable_formula", False):
        updates["DOCLING_ENABLE_FORMULA_ENRICHMENT"] = "1"
    if getattr(args, "disable_formula", False):
        updates["DOCLING_ENABLE_FORMULA_ENRICHMENT"] = "0"

    if getattr(args, "llm", None):
        updates["LLM_MODEL"] = args.llm
    if getattr(args, "embedding", None):
        updates["EMBEDDING_MODEL"] = args.embedding
        if not getattr(args, "dim", None):
            emb_lower = args.embedding.lower()
            if "embeddinggemma-2" in emb_lower:
                updates["EMBEDDING_DIM"] = "768"
            elif "qwen3-embedding:0.6b" in emb_lower:
                updates["EMBEDDING_DIM"] = "1024"
            elif "qwen3-embedding:4b" in emb_lower or "qwen3-embedding:latest" in emb_lower:
                updates["EMBEDDING_DIM"] = "2560"

    if getattr(args, "dim", None):
        updates["EMBEDDING_DIM"] = str(args.dim)

    if getattr(args, "asr", None):
        asr_val = args.asr.strip().lower()
        if not asr_val.startswith("whisper"):
            sys.exit(
                f"Error: only Whisper is currently supported for transcription (al momento è supportato solo Whisper) "
                f"(e.g. whisper_turbo_native, whisper_turbo, whisper_s2t). Received: {args.asr}"
            )
        updates["DOCLING_ASR_ENGINE"] = asr_val

    if getattr(args, "ollama_host", None):
        updates["OLLAMA_HOST"] = args.ollama_host

    if getattr(args, "chunk_size", None):
        updates["CHUNK_SIZE"] = str(args.chunk_size)
    if getattr(args, "chunk_overlap", None):
        updates["CHUNK_OVERLAP_SIZE"] = str(args.chunk_overlap)

    if not updates:
        print("No parameters specified. Use `ragcli config set --help`.")
        return

    upsert_global_env(updates)
    print("Configuration updated successfully (Configurazione aggiornata con successo):")
    for k, v in updates.items():
        print(f"  {k} = {v}")

    if getattr(args, "kb", None):
        env = global_env()
        registry = load_registry(env)
        kb = find_kb(registry, args.kb)
        if not kb:
            sys.exit(f"KB '{args.kb}' not found.")
        write_kb_env(kb, env)
        print(f"Local environment for KB '{args.kb}' synchronized.")


# Query and display available Ollama models.
def cmd_config_models(_args: argparse.Namespace) -> None:
    env = global_env()
    host = env.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    if "localhost" in host:
        host = host.replace("localhost", "127.0.0.1")
    try:
        res = requests.get(f"{host}/api/tags", timeout=5)
        res.raise_for_status()
        data = res.json()
        models = data.get("models", [])
    except Exception as exc:
        sys.exit(f"Unable to contact Ollama at {host}: {exc}")

    current_llm = env.get("LLM_MODEL", "")
    current_emb = env.get("EMBEDDING_MODEL", "")

    print(f"\nAvailable Ollama models ({host}):")
    print(f"{'NAME':<42} {'SIZE':<12} {'ACTIVE ROLE':<20}")
    print("-" * 76)
    for m in models:
        name = m.get("name", "")
        size_bytes = m.get("size", 0)
        size_gb = f"{size_bytes / (1024**3):.1f} GB" if size_bytes else "-"
        role = ""
        if name == current_llm:
            role = "[LLM ATTIVO]"
        elif name == current_emb:
            role = "[EMBEDDING ATTIVO]"
        print(f"{name:<42} {size_gb:<12} {role:<20}")

    print("\nAudio transcription model (ASR):")
    print(f"  Current engine: {env.get('DOCLING_ASR_ENGINE', 'whisper_turbo_native')} (Whisper only)")


# Unified MCP authentication management commands.
# Invariant: MCP_AUTH_TOKEN in config/global.env is the single source of truth
# for FastMCP, local CLI, web connectors (Claude, ChatGPT, Grok, Gemini), and systemd.
def _restart_mcp_service_if_active() -> None:
    """Restart knowledge-mcp systemd user service if currently active."""
    res = services.systemctl_user("is-active", "--quiet", "lightrag-mcp.service")
    if res.returncode == 0:
        services.systemctl_user("restart", "lightrag-mcp.service")
        print("  -> Servizio systemd 'lightrag-mcp.service' riavviato con successo.")


def _print_mcp_connector_help(token: str) -> None:
    env = global_env()
    host = env.get("MCP_BIND_HOST", "127.0.0.1")
    port = env.get("MCP_BIND_PORT", "8743")
    path = env.get("MCP_PATH", "/mcp")
    url = f"http://{host}:{port}{path}"

    print("\n" + "=" * 65)
    print("      CONFIGURAZIONE CONNETTORI MCP (CLAUDE, CHATGPT, GROK, GEMINI)")
    print("=" * 65)
    print(f"Endpoint locale:    {url}")
    print(f"Endpoint Tailscale: https://knowledge.tail0f7f67.ts.net{path}")
    print("\nEsempi di configurazione autenticazione:")
    print("1. Claude Web / ChatGPT Actions / Grok (Header Bearer):")
    print(f"   Authorization: Bearer {token}")
    print("\n2. Header personalizzato API Key:")
    print(f"   X-API-Key: {token}")
    print("\n3. cURL / Test rapido (il token non va mai messo nell'URL):")
    print(f'   curl -H "Authorization: Bearer {token}" https://knowledge.tail0f7f67.ts.net{path}')
    print("=" * 65 + "\n")


def cmd_auth_show(args: argparse.Namespace) -> None:
    env = global_env()
    token = env.get("MCP_AUTH_TOKEN", "").strip()
    if not token:
        print("Autenticazione MCP: DISABILITATA (accesso aperto a chiunque raggiunga la porta)")
        print("Per impostare una password: ragcli mcp auth set <password> oppure ragcli mcp auth generate")
        return

    plain = getattr(args, "plain", False)
    display_token = token if plain else (token[:4] + "*" * (len(token) - 8) + token[-4:] if len(token) > 8 else "****")
    print("Autenticazione MCP: ABILITATA [ATTIVA]")
    print(f"Password/Token unico: {display_token}")
    if not plain:
        print("(Usa `ragcli mcp auth show --plain` per vederla in chiaro)")
    _print_mcp_connector_help(token)


def cmd_auth_set(args: argparse.Namespace) -> None:
    token = args.password.strip()
    if not token:
        sys.exit("Errore: la password/token non può essere vuota. Per rimuoverla usa `ragcli mcp auth clear`.")
    upsert_global_env({"MCP_AUTH_TOKEN": token})
    print(f"Password MCP centralizzata aggiornata in config/global.env.")
    _restart_mcp_service_if_active()
    _print_mcp_connector_help(token)


def cmd_auth_generate(_args: argparse.Namespace) -> None:
    import secrets
    token = secrets.token_urlsafe(24)
    upsert_global_env({"MCP_AUTH_TOKEN": token})
    print("Generata nuova password crittografica sicura per MCP.")
    print(f"Password/Token unico: {token}")
    _restart_mcp_service_if_active()
    _print_mcp_connector_help(token)


def cmd_auth_clear(_args: argparse.Namespace) -> None:
    upsert_global_env({"MCP_AUTH_TOKEN": ""})
    print("Password MCP rimossa da config/global.env. L'accesso MCP è ora aperto.")
    _restart_mcp_service_if_active()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ragcli",
        description="Minimal CLI for managing WebUI-first LightRAG KBs with a shared runtime.",
    )
    sub = parser.add_subparsers(dest="cmd")

    job = sub.add_parser("job", help="ingestion jobs")
    job_sub = job.add_subparsers(dest="job_cmd", required=True)
    for name in ("status", "wait", "retry"):
        job_cmd = job_sub.add_parser(name)
        job_cmd.add_argument("job_id")
        job_cmd.add_argument("--kb", required=True)
        job_cmd.add_argument("--json", action="store_true")
        job_cmd.set_defaults(func=cmd_job)
    job_list = job_sub.add_parser("list")
    job_list.add_argument("--kb", required=True)
    job_list.add_argument("--stage")
    job_list.add_argument("--limit", type=int, default=50)
    job_list.add_argument("--json", action="store_true")
    job_list.set_defaults(func=cmd_job)

    doctor = sub.add_parser("doctor", help="verify runtime, config, services, and publishing")
    doctor.set_defaults(func=cmd_doctor)

    add = sub.add_parser("add", help="create a KB from a source directory")
    add.add_argument("name")
    add.add_argument("source_dir")
    add.add_argument("--port", type=int, help="porta del server LightRAG")
    add.set_defaults(func=cmd_add)

    list_cmd = sub.add_parser("list", help="list registered KBs")
    list_cmd.set_defaults(func=cmd_list)

    ingest = sub.add_parser("ingest", help="run one ingestion pass (folder sources and inbox)")
    ingest.add_argument("name")
    ingest.add_argument("--wait", action="store_true", help="run in the foreground and print the report")
    ingest.add_argument("--source", action="append", help="limit the pass to a source id (repeatable, with --wait)")
    ingest.add_argument("--no-inbox", action="store_true", help="skip the inbox (with --wait)")
    ingest.add_argument("--json", action="store_true")
    ingest.set_defaults(func=cmd_ingest)

    inbox = sub.add_parser("inbox", help="append-only inbox management")
    inbox_sub = inbox.add_subparsers(dest="inbox_cmd", required=True)
    inbox_send = inbox_sub.add_parser("send", help="queue a file, a ZIP or a directory")
    inbox_send.add_argument("name")
    inbox_send.add_argument("path")
    inbox_send.add_argument("--no-start", action="store_true", help="queue only, do not start the worker")
    inbox_send.add_argument("--json", action="store_true")
    inbox_send.set_defaults(func=cmd_inbox_send)
    inbox_consume = inbox_sub.add_parser("consume", help="start the worker for pending batches")
    inbox_consume.add_argument("name")
    inbox_consume.add_argument("--json", action="store_true")
    inbox_consume.set_defaults(func=cmd_inbox_consume)
    inbox_status = inbox_sub.add_parser("status", help="show inbox queue status")
    inbox_status.add_argument("name")
    inbox_status.add_argument("--json", action="store_true")
    inbox_status.set_defaults(func=cmd_inbox_status)

    source = sub.add_parser("source", help="manage source folders of a KB")
    source_sub = source.add_subparsers(dest="source_cmd", required=True)
    source_add = source_sub.add_parser("add", help="register a source folder")
    source_add.add_argument("name")
    source_add.add_argument("path")
    source_add.add_argument("--label", help="display name of the source")
    source_add.add_argument("--json", action="store_true")
    source_add.set_defaults(func=cmd_source_add)
    source_rm = source_sub.add_parser("remove", aliases=["rm"], help="detach a source by id")
    source_rm.add_argument("name")
    source_rm.add_argument("source_id")
    source_rm.add_argument("--json", action="store_true")
    source_rm.set_defaults(func=cmd_source_rm)
    source_ls = source_sub.add_parser("list", aliases=["ls"], help="list sources with file counts")
    source_ls.add_argument("name")
    source_ls.add_argument("--json", action="store_true")
    source_ls.set_defaults(func=cmd_source_ls)
    source_ingest = source_sub.add_parser("ingest", help="ingest only the given source ids, in the foreground")
    source_ingest.add_argument("name")
    source_ingest.add_argument("source_ids", nargs="+")
    source_ingest.add_argument("--json", action="store_true")
    source_ingest.set_defaults(func=cmd_source_ingest)

    worker = sub.add_parser("_worker_run", help=argparse.SUPPRESS)
    worker.add_argument("name")
    worker.set_defaults(func=cmd_worker_run)

    exclude_help = (
        "Pattern glob stile fnmatch.\n"
        "- use a trailing / to indicate a folder: 'agent/' takes the folder top-level 'agent'; 'notes/agent/' prende quel path specifico\n"
        "- use * for files: '*.txt' takes all files txt; 'agent*' takes agent.md, agentico.md, ecc.\n"
        "- use '.*/' for all top-level folders beginning with . in the KB; expansion happens live with 'ls --expanded' and during ingest\n"
        "- you can pass multiple patterns in one instruction, separated by spaces, commas, or ';'\n"
        "- always quote patterns to prevent shell expansion\n"
        "Esempi:\n"
        "  ragcli exclude add second-brain '*.txt' 'agent*' 'agent/'\n"
        "  ragcli exclude add second-brain '.*/'\n"
        "  ragcli exclude add second-brain '.claude/' '.codex/' '.git/'\n"
        "  ragcli exclude ls second-brain --expanded\n"
    )
    exclude = sub.add_parser(
        "exclude",
        help="exclusion pattern management",
        description=exclude_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    exclude_sub = exclude.add_subparsers(dest="exclude_cmd", required=True)
    ex_add = exclude_sub.add_parser(
        "add",
        help="add one or more exclusion patterns",
        description=exclude_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ex_add.add_argument("name")
    ex_add.add_argument("pattern", nargs="+")
    ex_add.set_defaults(func=cmd_exclude_add)
    ex_rm = exclude_sub.add_parser(
        "rm",
        help="remove one or more exclusion patterns",
        description=exclude_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ex_rm.add_argument("name")
    ex_rm.add_argument("pattern", nargs="+")
    ex_rm.set_defaults(func=cmd_exclude_rm)
    ex_ls = exclude_sub.add_parser(
        "ls",
        help="list KB exclusion patterns",
        description=exclude_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ex_ls.add_argument("name")
    ex_ls.add_argument("--expanded", action="store_true", help="show the live expansion of excluded folders")
    ex_ls.set_defaults(func=cmd_exclude_ls)

    start = sub.add_parser("start", help="start a KB server")
    start.add_argument("name")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop a KB server")
    stop.add_argument("name")
    stop.set_defaults(func=cmd_stop)

    remove = sub.add_parser("remove", help="delete the entire KB while preserving source files")
    remove.add_argument("name")
    remove.set_defaults(func=cmd_remove)

    mcp = sub.add_parser("mcp", help="register or remove MCP in supported clients")
    mcp_sub = mcp.add_subparsers(dest="mcp_cmd", required=True)
    mcp_add = mcp_sub.add_parser("add", help="register the KB MCP")
    mcp_add.add_argument("name")
    mcp_add.add_argument("--client", choices=list(mcp_clients.CLIENTS) + ["all"], default="claude-code")
    mcp_add.add_argument("--print-only", action="store_true")
    mcp_add.set_defaults(func=cmd_mcp_add)
    mcp_rm = mcp_sub.add_parser("rm", help="remove the KB MCP")
    mcp_rm.add_argument("name")
    mcp_rm.add_argument("--client", choices=list(mcp_clients.REMOVERS) + ["all"], default="claude-code")
    mcp_rm.set_defaults(func=cmd_mcp_rm)

    # Dedicated unified auth subcommand for MCP
    auth = mcp_sub.add_parser("auth", help="gestione centralizzata della password/token di protezione MCP")
    auth_sub = auth.add_subparsers(dest="auth_cmd")
    auth.set_defaults(func=cmd_auth_show)

    auth_show = auth_sub.add_parser("show", help="mostra stato e password MCP")
    auth_show.add_argument("--plain", action="store_true", help="mostra la password in chiaro")
    auth_show.set_defaults(func=cmd_auth_show)

    auth_set = auth_sub.add_parser("set", help="imposta una password per proteggere l'MCP online")
    auth_set.add_argument("password", help="la password/token da impostare")
    auth_set.set_defaults(func=cmd_auth_set)

    auth_gen = auth_sub.add_parser("generate", help="genera una password crittografica sicura per l'MCP")
    auth_gen.set_defaults(func=cmd_auth_generate)

    auth_clear = auth_sub.add_parser("clear", help="rimuove la password (accesso libero)")
    auth_clear.set_defaults(func=cmd_auth_clear)

    kb_group = sub.add_parser("kb", help="knowledge base settings")
    kb_sub = kb_group.add_subparsers(dest="kb_cmd", required=True)
    for parent, label in ((kb_sub, "data-location"),):
        loc = parent.add_parser(label, help="move a KB's data between knowledge-os and a source folder")
        loc.add_argument("name")
        loc.add_argument("--to", choices=["repo", "source"], help="destination (default: toggle)")
        loc.add_argument("--source", help="source folder that will host the data (required with several sources)")
        loc.add_argument("--dry-run", action="store_true")
        loc.add_argument("--json", action="store_true")
        loc.set_defaults(func=cmd_move)

    move = sub.add_parser("move", help="move a KB's data between knowledge-os and a source folder")
    move.add_argument("name")
    move.add_argument("--to", choices=["repo", "source"], help="destination (default: toggle)")
    move.add_argument("--source", help="source folder that will host the data (required with several sources)")
    move.add_argument("--dry-run", action="store_true", help="check the move without changing anything")
    move.set_defaults(func=cmd_move)

    service = sub.add_parser("service", help="manage Linux systemd --user services")
    service_sub = service.add_subparsers(dest="service_cmd", required=True)
    service_status = service_sub.add_parser("status", help="show service status")
    service_status.add_argument("name", nargs="?")
    service_status.set_defaults(func=cmd_service_status)
    service_restart = service_sub.add_parser("restart", help="riavvia un servizio")
    service_restart.add_argument("name", nargs="?")
    service_restart.set_defaults(func=cmd_service_restart)
    service_logs = service_sub.add_parser("logs", help="mostra log di un servizio")
    service_logs.add_argument("name", nargs="?")
    service_logs.set_defaults(func=cmd_service_logs)
    service_install = service_sub.add_parser("install-units", help="installa le unit systemd --user del repo")
    service_install.set_defaults(func=cmd_service_install_units)

    # memory subcommand
    mem_parser = sub.add_parser("memory", help="manage memory for coding agents")
    mem_sub = mem_parser.add_subparsers(dest="memory_cmd", required=True)

    rem_p = mem_sub.add_parser("remember", help="save a new memory")
    rem_p.add_argument("kb", help="KB name")
    rem_p.add_argument("content", help="memory text")
    rem_p.add_argument("--type", choices=("preference", "decision", "episodic", "fact"), default="fact")
    rem_p.add_argument("--title", default="")
    rem_p.add_argument("--tags", default="", help="comma-separated tags")
    rem_p.add_argument("--supersedes", default=None, help="ID memoria superata da questa")
    rem_p.set_defaults(func=cmd_memory_remember)

    rec_p = mem_sub.add_parser("recall", help="search active memories")
    rec_p.add_argument("kb_legacy", nargs="?", help=argparse.SUPPRESS)
    rec_p.add_argument("query", nargs="+", help="termini di ricerca")
    rec_p.add_argument("--kb", action="append", default=None, help="limita la ricerca a una o più KB (default: tutte)")
    rec_p.add_argument("--type", choices=("preference", "decision", "episodic", "fact"), default=None)
    rec_p.add_argument("--all", action="store_true", help="include superseded/deprecated memories")
    rec_p.add_argument("--top-k", type=int, default=5)
    rec_p.set_defaults(func=cmd_memory_recall)

    drm_p = mem_sub.add_parser("dream", help="run the consolidation and deduplication cycle (Dreaming)")
    drm_p.add_argument("kb", nargs="?", help="KB name (default: tutte se si usa --all)")
    drm_p.add_argument("--all", action="store_true", help="esegue il ciclo su tutte le KB")
    drm_p.add_argument("--dry-run", action="store_true")
    drm_p.set_defaults(func=cmd_memory_dream)

    lst_p = mem_sub.add_parser("list", help="list KB memories")
    lst_p.add_argument("kb", help="KB name")
    lst_p.add_argument("--type", choices=("preference", "decision", "episodic", "fact"), default=None)
    lst_p.add_argument("--status", choices=("active", "superseded", "deprecated"), default=None)
    lst_p.set_defaults(func=cmd_memory_list)

    dep_p = mem_sub.add_parser("deprecate", help="invalidate or supersede a memory")
    dep_p.add_argument("kb", help="KB name")
    dep_p.add_argument("memory_id", help="ID della memoria")
    dep_p.add_argument("--reason", default="", help="deprecation reason")
    dep_p.add_argument("--superseded-by", default=None, help="ID of the new memory that replaces it")
    dep_p.set_defaults(func=cmd_memory_deprecate)

    retrieve = sub.add_parser("retrieve", help="retrieval gerarchico su tutte le KB o su KB selezionate")
    retrieve_sub = retrieve.add_subparsers(dest="retrieve_cmd", required=True)
    retrieve_ask = retrieve_sub.add_parser("ask", help="interroga una o tutte le KB")
    retrieve_ask.add_argument("query")
    retrieve_ask.add_argument("--kb", action="append", default=None)
    retrieve_ask.add_argument("--strategy", default="balanced")
    retrieve_ask.add_argument("--format", default=None)
    retrieve_ask.set_defaults(func=cmd_retrieve_ask)
    retrieve_memory = retrieve_sub.add_parser("memory", help="search memories across all KBs or with --kb")
    retrieve_memory.add_argument("query")
    retrieve_memory.add_argument("--kb", action="append", default=None)
    retrieve_memory.add_argument("--type", choices=("preference", "decision", "episodic", "fact"), default=None)
    retrieve_memory.add_argument("--all", action="store_true")
    retrieve_memory.add_argument("--top-k", type=int, default=5)
    retrieve_memory.set_defaults(func=cmd_memory_recall)
    # import-archive command
    imp_p = sub.add_parser("import-archive", help="import and normalize chat/data archives (ZIP/JSON)")
    imp_p.add_argument("kb", help="KB name")
    imp_p.add_argument("archive", help="ZIP file or exported folder path")
    imp_p.add_argument("--format", choices=("auto", "chatgpt", "claude", "gemini", "generic"), default="auto")
    imp_p.set_defaults(func=cmd_import_archive)

    # hook command
    hook_parser = sub.add_parser("hook", help="hooks for coding agents (pre-prompt and post-session)")
    hook_sub = hook_parser.add_subparsers(dest="hook_cmd", required=True)

    hinj_p = hook_sub.add_parser("inject", help="generate a pre-prompt context block from active memories")
    hinj_p.add_argument("--kb", default="agent-memory", help="nome KB")
    hinj_p.add_argument("--prompt", default="", help="prompt utente")
    hinj_p.set_defaults(func=cmd_hook_inject)

    hcap_p = hook_sub.add_parser("capture", help="capture post-session logs/decisions")
    hcap_p.add_argument("--kb", default="agent-memory", help="nome KB")
    hcap_p.add_argument("--transcript", default=None)
    hcap_p.add_argument("--text", default=None)
    hcap_p.add_argument("--session-id", default=None)
    hcap_p.set_defaults(func=cmd_hook_capture)

    hinst_p = hook_sub.add_parser("install", help="install hook configuration in the specified client")
    hinst_p.add_argument("--client", choices=("claude-code", "cursor", "codex", "antigravity"), default="claude-code")
    hinst_p.add_argument("--kb", default="agent-memory")
    hinst_p.add_argument("--project-dir", default=None)
    hinst_p.set_defaults(func=cmd_hook_install)

    cfg_parser = sub.add_parser("config", help="centralized configuration panel (pipeline phases, models, and runtime)")
    cfg_sub = cfg_parser.add_subparsers(dest="config_cmd")
    cfg_parser.set_defaults(func=cmd_config_show)

    cfg_show = cfg_sub.add_parser("show", help="show global configuration organized by pipeline phases")
    cfg_show.set_defaults(func=cmd_config_show)

    cfg_set = cfg_sub.add_parser("set", help="modify parameters and enable/disable pipeline elements")
    cfg_set.add_argument("--enable-text", action="store_true", help="enable pipeline testo/codice")
    cfg_set.add_argument("--disable-text", action="store_true", help="disenable pipeline testo/codice")
    cfg_set.add_argument("--enable-pdf", action="store_true", help="enable pipeline PDF")
    cfg_set.add_argument("--disable-pdf", action="store_true", help="disenable pipeline PDF")
    cfg_set.add_argument("--enable-images", action="store_true", help="enable pipeline immagini")
    cfg_set.add_argument("--disable-images", action="store_true", help="disenable pipeline immagini")
    cfg_set.add_argument("--enable-office", action="store_true", help="enable pipeline office/ODF")
    cfg_set.add_argument("--disable-office", action="store_true", help="disenable pipeline office/ODF")
    cfg_set.add_argument("--enable-audio", action="store_true", help="enable pipeline audio/video")
    cfg_set.add_argument("--disable-audio", action="store_true", help="disenable pipeline audio/video")
    cfg_set.add_argument("--enable-tables", action="store_true", help="attiva estrazione tabelle Docling")
    cfg_set.add_argument("--disable-tables", action="store_true", help="disattiva estrazione tabelle Docling")
    cfg_set.add_argument("--enable-code", action="store_true", help="attiva arricchimento codice Docling")
    cfg_set.add_argument("--disable-code", action="store_true", help="disattiva arricchimento codice Docling")
    cfg_set.add_argument("--enable-formula", action="store_true", help="attiva arricchimento formule Docling")
    cfg_set.add_argument("--disable-formula", action="store_true", help="disattiva arricchimento formule Docling")
    cfg_set.add_argument("--llm", help="modello LLM (es. qwen3.5:4b)")
    cfg_set.add_argument("--embedding", help="modello embedding (es. embeddinggemma-2:latest)")
    cfg_set.add_argument("--dim", type=int, help="embedding dimension (e.g. 768 for embeddinggemma-2)")
    cfg_set.add_argument("--asr", help="modello trascrizione (solo Whisper: whisper_turbo_native, whisper_turbo, whisper_s2t)")
    cfg_set.add_argument("--ollama-host", help="URL host Ollama (es. http://localhost:11434)")
    cfg_set.add_argument("--chunk-size", type=int, help="dimensione chunk in token")
    cfg_set.add_argument("--chunk-overlap", type=int, help="sovrapposizione chunk in token")
    cfg_set.add_argument("--kb", help="optional KB name to synchronize its .env")
    cfg_set.set_defaults(func=cmd_config_set)

    cfg_models = cfg_sub.add_parser("models", help="elenca i modelli disponibili su Ollama e lo stato attivo")
    cfg_models.set_defaults(func=cmd_config_models)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.cmd is None:
        args.func = cmd_status
    # User errors (unknown KB, bad path, busy lock) are raised as ValueError by tools.*
    # and shown as one line: a traceback would hide the message from the user.
    try:
        args.func(args)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}")


if __name__ == "__main__":
    main()
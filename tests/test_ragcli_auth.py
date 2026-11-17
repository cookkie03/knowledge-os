from __future__ import annotations

import argparse
from pathlib import Path

import tools.system as kb_registry
from ragcli import cmd_auth_clear, cmd_auth_generate, cmd_auth_set, cmd_auth_show


def test_ragcli_auth_lifecycle(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "global.env"
    env_file.write_text("MCP_BIND_HOST=127.0.0.1\nMCP_BIND_PORT=8743\n", encoding="utf-8")

    monkeypatch.setattr(kb_registry, "CONFIG", env_file)
    monkeypatch.setattr("ragcli._restart_mcp_service_if_active", lambda: None)

    # 1. Initially disabled
    assert not kb_registry.global_env().get("MCP_AUTH_TOKEN")

    # 2. Set explicit password
    args_set = argparse.Namespace(password="my-secure-password-42")
    cmd_auth_set(args_set)
    assert kb_registry.global_env().get("MCP_AUTH_TOKEN") == "my-secure-password-42"

    # 3. Generate random secure password
    cmd_auth_generate(argparse.Namespace())
    generated = kb_registry.global_env().get("MCP_AUTH_TOKEN")
    assert generated and len(generated) >= 24

    # 4. Clear password
    cmd_auth_clear(argparse.Namespace())
    assert not kb_registry.global_env().get("MCP_AUTH_TOKEN")

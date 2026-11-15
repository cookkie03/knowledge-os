from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import ragcli


def test_service_parser_accepts_status_restart_logs() -> None:
    parser = ragcli.build_parser()

    status_args = parser.parse_args(["service", "status"])
    restart_args = parser.parse_args(["service", "restart", "lightrag-mcp@.service"])
    logs_args = parser.parse_args(["service", "logs", "lightrag-mcp@.service"])

    assert status_args.service_cmd == "status"
    assert restart_args.service_cmd == "restart"
    assert logs_args.service_cmd == "logs"


def test_doctor_report_requires_executable_runtime_and_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "python"
    runtime.write_text("#!/bin/sh\n")
    runtime.chmod(0o644)
    server = tmp_path / "lightrag-server"
    server.write_text("#!/bin/sh\n")
    server.chmod(0o755)

    monkeypatch.setattr(
        ragcli,
        "global_env",
        lambda: {
            "ENGINE_PYTHON": str(runtime),
            "LIGHTRAG_SERVER_BIN": str(server),
        },
    )
    monkeypatch.setattr(
        ragcli,
        "_systemctl_user",
        lambda *args: type("R", (), {"returncode": 3, "stdout": "inactive\n", "stderr": ""})(),
    )

    report = ragcli._doctor_report()

    assert report[:2] == [
        {"name": "runtime", "ok": False, "details": f"not executable ({runtime})"},
        {"name": "server", "ok": True, "details": str(server)},
    ]


def test_doctor_reports_missing_runtime(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        ragcli,
        "_doctor_report",
        lambda: [{"name": "runtime", "ok": False, "details": "missing"}],
    )

    with pytest.raises(SystemExit) as exc:
        ragcli.cmd_doctor(SimpleNamespace())

    assert exc.value.code == 1
    assert "runtime: missing" in capsys.readouterr().out


def test_doctor_allows_healthy_report_with_down_in_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        ragcli,
        "_doctor_report",
        lambda: [{"name": "runtime", "ok": True, "details": "/tmp/downloads/python"}],
    )

    ragcli.cmd_doctor(SimpleNamespace())

    assert "runtime: /tmp/downloads/python" in capsys.readouterr().out


def test_service_status_uses_systemctl_user(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], capture_output: bool = True, text: bool = True, check: bool = False):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "active\n", "stderr": ""})()

    monkeypatch.setattr(ragcli.subprocess, "run", fake_run)

    ragcli.cmd_service_status(SimpleNamespace(name="lightrag-server@second-brain.service"))

    assert calls[0][:2] == ["systemctl", "--user"]
    assert "active" in capsys.readouterr().out


def test_service_status_reports_installed_templates_when_no_public_kb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    systemd_dir = tmp_path / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True)
    for unit in ("lightrag-server@.service", "lightrag-watchdog@.service", "lightrag-mcp@.service"):
        (systemd_dir / unit).write_text("[Unit]\n")

    monkeypatch.setattr(ragcli, "global_env", lambda: {"PUBLIC_MCP_KB": ""})
    monkeypatch.setattr(ragcli.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(
        ragcli,
        "_systemctl_user",
        lambda *args: type("R", (), {"returncode": 0, "stdout": "active\n", "stderr": ""})(),
    )

    ragcli.cmd_service_status(SimpleNamespace(name=None))

    output = capsys.readouterr().out
    assert "lightrag-server@.service: installed" in output
    assert "lightrag-watchdog@.service: installed" in output
    assert "lightrag-mcp@.service: installed" in output


def test_service_restart_uses_systemctl_user_for_specific_unit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], capture_output: bool = True, text: bool = True, check: bool = False):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(ragcli.subprocess, "run", fake_run)

    ragcli.cmd_service_restart(SimpleNamespace(name="lightrag-mcp@.service"))

    assert calls == [["systemctl", "--user", "restart", "lightrag-mcp@.service"]]
    assert "restarted lightrag-mcp@.service" in capsys.readouterr().out


def test_service_install_units_writes_repo_specific_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_root = tmp_path / "repo"
    systemd_src = repo_root / "systemd"
    systemd_src.mkdir(parents=True)
    (systemd_src / "lightrag-server@.service").write_text("ExecStart=__LIGHTRAG_SERVER_BIN__\n")
    (systemd_src / "lightrag-watchdog@.service").write_text("home=__HOME__\n")
    (systemd_src / "lightrag-mcp@.service").write_text("ExecStart=__ENGINE_PYTHON__ __REPO_ROOT__/mcp/lightrag_mcp.py\n")
    home = tmp_path / "home"
    home.mkdir()

    installed = ragcli.install_user_units(
        repo_root,
        home,
        env={
            "ENGINE_PYTHON": str(repo_root / ".venv/bin/python"),
            "LIGHTRAG_SERVER_BIN": str(repo_root / ".venv/bin/lightrag-server"),
        },
    )

    assert len(installed) == 3
    server_unit = (home / ".config" / "systemd" / "user" / "lightrag-server@.service").read_text()
    assert str(repo_root / ".venv/bin/lightrag-server") in server_unit
    assert "__LIGHTRAG_SERVER_BIN__" not in server_unit

    monkeypatch.setattr(ragcli, "install_user_units", lambda *_args, **_kwargs: installed)
    monkeypatch.setattr(ragcli, "global_env", lambda: {"ENGINE_PYTHON": "x", "LIGHTRAG_SERVER_BIN": "y"})
    monkeypatch.setattr(ragcli.Path, "home", classmethod(lambda cls: home))

    ragcli.cmd_service_install_units(SimpleNamespace())

    assert "installed user units" in capsys.readouterr().out


def test_doctor_reports_service_status_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ragcli,
        "global_env",
        lambda: {
            "ENGINE_PYTHON": "/tmp/runtime/python",
            "LIGHTRAG_SERVER_BIN": "/tmp/runtime/lightrag-server",
            "PUBLIC_MCP_KB": "",
        },
    )
    monkeypatch.setattr(
        ragcli,
        "_systemctl_user",
        lambda *args: type("R", (), {"returncode": 0, "stdout": "active\n", "stderr": ""})(),
    )
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(ragcli.os, "access", lambda path, mode: True)

    report = ragcli._doctor_report()

    service_row = next(item for item in report if item["name"] == "lightrag-server@.service")
    assert service_row == {
        "name": "lightrag-server@.service",
        "ok": True,
        "details": "installed",
    }


def test_status_units_expand_public_kb_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ragcli, "load_registry", lambda env: {"kbs": [{"name": "second-brain"}]})
    monkeypatch.setattr(ragcli, "find_kb", lambda registry, name: {"name": name})

    units = ragcli._status_linux_units({"PUBLIC_MCP_KB": "second-brain"})

    assert units == [
        "lightrag-server@second-brain.service",
        "lightrag-watchdog@second-brain.service",
        "lightrag-mcp@second-brain.service",
    ]


def test_health_returns_false_when_requests_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ragcli, "requests", None)

    assert ragcli.health(9621) is False

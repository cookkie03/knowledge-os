"""Linux systemd user services, doctor report and Tailscale helpers (no printing, no argv)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from tools.kb import find_kb, load_registry
from tools.system import global_env

LINUX_SYSTEMD_UNITS = (
    "lightrag-server@.service",
    "lightrag-watchdog@.service",
    "lightrag-mcp.service",
)


def _completed_process(args: list[str], returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)
#
# What this does: creates a synthetic CompletedProcess object for testing purposes,
# allowing tests to simulate subprocess results without actually running commands.
# Inputs/Outputs: args (list of command args), returncode (int), stdout (str, default ""),
# stderr (str, default "") -> CompletedProcess instance


def normalize_service_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ValueError("Service name is required.")
    if "." not in normalized:
        normalized = f"{normalized}.service"
    return normalized
#
# What this does: normalizes a service name by stripping whitespace and ensuring it has a .service
# extension. If the name is empty, exits the program. If no dot is present, appends .service.
# Inputs/Outputs: name (str) -> normalized str with .service suffix


def _systemd_user_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"
#
# What this does: returns the path to the user-level systemd config directory
# (~/.config/systemd/user). This is where user-specific systemd units are installed.
# Inputs/Outputs: none -> Path to the user systemd directory


def _configured_public_kb(env: dict[str, str]) -> str | None:
    kb_name = env.get("PUBLIC_MCP_KB", "").strip()
    if not kb_name:
        return None
    try:
        registry = load_registry(env)
    except Exception:
        return None
    return kb_name if find_kb(registry, kb_name) else None
#
# What this does: checks the environment dict for a PUBLIC_MCP_KB setting, validates it exists
# in the registry, and returns the kb name if found. Used to determine if a public KB is configured.
# Inputs/Outputs: env (dict of environment variables) -> str|None knowledge base name or None


def status_linux_units(env: dict[str, str] | None = None) -> list[str]:
    env = env or global_env()
    public_kb = _configured_public_kb(env)
    if public_kb:
        return [
            f"lightrag-server@{public_kb}.service",
            f"lightrag-watchdog@{public_kb}.service",
            "lightrag-mcp.service",
        ]
    return list(LINUX_SYSTEMD_UNITS)
#
# What this does: returns the list of Linux systemd user service units based on whether a public
# knowledge base is configured. If a public KB exists, returns KB-specific service names; otherwise
# returns the default LINUX_SYSTEMD_UNITS tuple.
# Inputs/Outputs: env (optional dict) -> list of service unit name strings


def manageable_linux_units(env: dict[str, str] | None = None) -> list[str]:
    env = env or global_env()
    public_kb = _configured_public_kb(env)
    units: list[str] = []
    if public_kb:
        units = [
            f"lightrag-server@{public_kb}.service",
            f"lightrag-watchdog@{public_kb}.service",
            "lightrag-mcp.service",
        ]
    return units
#
# What this does: returns the list of manageable Linux systemd user service units based on whether
# a public knowledge base is configured. Includes KB-specific services when a public KB is set,
# otherwise returns default units. Used for service management operations.
# Inputs/Outputs: env (optional dict) -> list of service unit name strings


def _unit_template_context(repo_root: Path, home: Path, env: dict[str, str]) -> dict[str, str]:
    return {
        "__REPO_ROOT__": str(repo_root),
        "__ENGINE_PYTHON__": env["ENGINE_PYTHON"],
        "__LIGHTRAG_SERVER_BIN__": env["LIGHTRAG_SERVER_BIN"],
        "__HOME__": str(home),
    }
#
# What this does: creates a template context dictionary with repository root, engine Python path,
# lightrag server binary path, and home directory. Used when generating systemd unit files
# with substituted tokens.
# Inputs/Outputs: repo_root (Path), home (Path), env (dict) -> dict of template token values


def install_user_units(repo_root: Path, home: Path, env: dict[str, str] | None = None) -> list[Path]:
    env = env or global_env()
    systemd_dir = home / ".config" / "systemd" / "user"
    systemd_dir.mkdir(parents=True, exist_ok=True)
    context = _unit_template_context(repo_root, home, env)
    installed: list[Path] = []
    unit_names = list(LINUX_SYSTEMD_UNITS)
    src_dir = repo_root / "deploy" / "systemd"
    if not src_dir.is_dir():
        src_dir = repo_root / "systemd"
    for optional in ("lightrag-dreaming.service", "lightrag-dreaming.timer"):
        if (src_dir / optional).is_file():
            unit_names.append(optional)
    for name in unit_names:
        src = src_dir / name
        text = src.read_text()
        for token, value in context.items():
            text = text.replace(token, value)
        dst = systemd_dir / name
        dst.write_text(text)
        installed.append(dst)
    return installed
#
# What this does: installs user-level systemd unit files from the deploy/systemd directory into
# the user's systemd config directory. Copies unit files, substitutes template tokens with
# environment values, and supports optional dreaming timer/service additions. Returns list of
# installed file paths.
# Inputs/Outputs: repo_root (Path), home (Path), env (optional dict) -> list of installed Path objects


def configure_tailscale_funnel(bind_host: str, bind_port: str, public_port: str) -> list[list[str]]:
    return [
        ["tailscale", "serve", "--bg", public_port, f"http://{bind_host}:{bind_port}"],
        ["tailscale", "funnel", "--bg", public_port],
    ]


def systemctl_user(*args: str) -> subprocess.CompletedProcess[str]:
    cmd = ["systemctl", "--user", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return _completed_process(cmd, 127, stderr="systemctl not found")
#
# What this does: runs systemctl --user commands and returns the result. Provides a wrapper
# that handles the FileNotFoundError if systemctl is not available, returning a completed process
# with return code 127 and stderr message. Used as a base for other systemd status functions.
# Inputs/Outputs: *args (str) -> CompletedProcess with return code and output
#
# What this this does: runs systemctl --user commands and returns the result. Provides a wrapper
# that handles the FileNotFoundError if systemctl is not available, returning a completed process
# with return code 127 and stderr message.
# Inputs/Outputs: *args (str) -> CompletedProcess with return code and output


def journalctl_user(*args: str) -> subprocess.CompletedProcess[str]:
    cmd = ["journalctl", "--user", *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return _completed_process(cmd, 127, stderr="journalctl not found")


def _template_unit_status(unit: str) -> dict[str, object]:
    unit_file = _systemd_user_dir() / unit
    installed = unit_file.is_file()
    return {
        "name": unit,
        "ok": installed,
        "details": "installed" if installed else "missing",
    }


def service_status(unit: str) -> dict[str, object]:
    if unit.endswith("@.service"):
        return _template_unit_status(unit)
    result = systemctl_user("is-active", unit)
    details = result.stdout.strip() or result.stderr.strip() or "unknown"
    return {
        "name": unit,
        "ok": result.returncode == 0 and details == "active",
        "details": details,
    }


def _funnel_status(env: dict[str, str]) -> dict[str, object]:
    public_url = env.get("SHIM_PUBLIC_URL", "").strip()
    if not public_url or "your-node.your-tailnet.ts.net" in public_url:
        return {
            "name": "shim_public_url",
            "ok": False,
            "details": public_url or "missing",
        }

    tailscale_bin = shutil.which("tailscale")
    if not tailscale_bin:
        return {"name": "funnel", "ok": False, "details": "tailscale not found"}

    result = subprocess.run([tailscale_bin, "funnel", "status"], capture_output=True, text=True)
    output = result.stdout.strip() or result.stderr.strip() or "unknown"
    public_port = env.get("TAILSCALE_FUNNEL_PORT", "8443")
    ok = result.returncode == 0 and (public_url in output or public_port in output)
    return {"name": "funnel", "ok": ok, "details": output}


def doctor_report() -> list[dict[str, object]]:
    env = global_env()
    engine_python = Path(env["ENGINE_PYTHON"]).expanduser()
    server_bin = Path(env["LIGHTRAG_SERVER_BIN"]).expanduser()

    def _runtime_check(path: Path) -> dict[str, object]:
        if not path.is_file():
            return {"ok": False, "details": "missing"}
        if not os.access(path, os.X_OK):
            return {"ok": False, "details": f"not executable ({path})"}
        return {"ok": True, "details": str(path)}

    runtime = _runtime_check(engine_python)
    server = _runtime_check(server_bin)
    report = [
        {
            "name": "runtime",
            "ok": runtime["ok"],
            "details": runtime["details"],
        },
        {
            "name": "server",
            "ok": server["ok"],
            "details": server["details"],
        },
    ]
    for unit in status_linux_units(env):
        report.append(service_status(unit))
    report.append(_funnel_status(env))
    return report

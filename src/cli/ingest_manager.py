from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
ENGINE_DIR = HOME / ".venv"
INGEST_SCRIPT = HOME / "bin" / "ingest.py"


def run_ingest(
    kb_name: str,
    source_folder: str | None = None,
    force: bool = False,
    pipelines: str | None = None,
    no_text: bool = False,
    no_pdf: bool = False,
    no_images: bool = False,
    no_office: bool = False,
    no_audio: bool = False,
    background: bool = True,
) -> dict:
    """Run the ingestion pipeline for a KB."""
    engine_python = ENGINE_DIR / "bin" / "python"
    if not engine_python.exists():
        engine_python = Path("python3")

    cmd = [str(engine_python), str(INGEST_SCRIPT), "--kb", kb_name]

    if force:
        cmd.append("--force")
    if pipelines:
        cmd.extend(["--pipelines", pipelines])
    if no_text:
        cmd.append("--no-text")
    if no_pdf:
        cmd.append("--no-pdf")
    if no_images:
        cmd.append("--no-images")
    if no_office:
        cmd.append("--no-office")
    if no_audio:
        cmd.append("--no-audio")
    if source_folder:
        cmd.extend(["--source-folder", source_folder])

    if background:
        # Run in background
        log_path = Path(f"./data/{kb_name}/ingest.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            cmd,
            stdout=log_path.open("a"),
            stderr=log_path.open("a"),
            start_new_session=True,
        )
        return {"ok": True, "status": "queued", "command": " ".join(cmd)}
    else:
        # Run synchronously
        result = subprocess.run(cmd, capture_output=True, text=True)
        return {
            "ok": result.returncode == 0,
            "status": "completed" if result.returncode == 0 else "failed",
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
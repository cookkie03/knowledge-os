#!/usr/bin/env python3
"""Run the ingestion pipeline for a KB.

This module now delegates to the centralized ingest_pipeline module for
all ingestion operations. This ensures that all consumers use the same
pipeline logic with centralized path validation, atomic writes, and
safety guards.

Deprecated: Use ingest_pipeline.run_ingest_pipeline instead.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent


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
    """Run the ingestion pipeline for a KB.

    Deprecated: Use ingest_pipeline.run_ingest_pipeline instead.
    This function delegates to the centralized pipeline for consistency.
    """
    from src.cli.ingest_pipeline import run_ingest_pipeline

    return run_ingest_pipeline(
        kb_name=kb_name,
        source_folder=source_folder,
        force=force,
        pipelines=pipelines,
        no_text=no_text,
        no_pdf=no_pdf,
        no_images=no_images,
        no_office=no_office,
        no_audio=no_audio,
        background=background,
    )
"""Resolve optional evaluation data and generated reports outside the source tree."""

from __future__ import annotations

import os
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent
MATERIALS_ROOT = Path(
    os.environ.get("CHAIS_MATERIALS_DIR", SOURCE_ROOT.parent / "competition-materials")
).expanduser()
DATA_ROOT = MATERIALS_ROOT / "local-data"
OUTPUT_ROOT = MATERIALS_ROOT / "local-state" / "eval_out"


def data_path(name: str) -> Path:
    """Return external evaluation data when available, with source-tree fallback."""

    external = DATA_ROOT / name
    if external.exists():
        return external
    return SOURCE_ROOT / name


def output_root() -> Path:
    """Use the sibling materials directory when present; otherwise use a local fallback."""

    if MATERIALS_ROOT.exists():
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        return OUTPUT_ROOT
    fallback = SOURCE_ROOT / "eval_out"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback

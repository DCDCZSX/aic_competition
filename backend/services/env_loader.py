"""Load deployment configuration from the source tree or sibling materials."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def load_project_environment(source_root: Optional[Path] = None) -> None:
    """Load the first available project ``.env`` files without overriding env vars.

    The competition source tree keeps secrets outside the repository.  Local
    development therefore checks the sibling materials directory after the
    conventional source locations.
    """

    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    root = Path(source_root or Path(__file__).resolve().parents[2])
    candidates = [
        Path(os.environ["CHAIS_ENV_FILE"]) if os.environ.get("CHAIS_ENV_FILE") else None,
        root / ".env",
        root / "backend" / ".env",
        root.parent / "competition-materials" / "local-config" / ".env",
    ]
    loaded: set[Path] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        path = candidate.expanduser().resolve()
        if path in loaded or not path.is_file():
            continue
        load_dotenv(path, override=False)
        loaded.add(path)

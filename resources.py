"""Resolve data files in either a source checkout or an installed wheel."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir(
    name: str,
    *,
    module_dir: Path | None = None,
    prefix: Path | None = None,
) -> Path:
    if override := os.environ.get("EAGLE_BROWSE_DATA_DIR"):
        return Path(override).expanduser() / name
    source = (module_dir or Path(__file__).resolve().parent) / name
    if source.is_dir():
        return source
    return (prefix or Path(sys.prefix)) / "share/eagle-browse" / name

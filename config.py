"""Load Eagle Browse paths from a TOML config file.

Search order (later files overlay earlier ones):

  1. ``<library-parent>/eagle-browse.toml`` — usually the Dropbox vault
  2. ``~/.config/eagle-browse/config.toml`` — per-machine overlay
  3. ``$EAGLE_BROWSE_CONFIG`` — explicit file

Keys (paths relative to the file that defined them, unless absolute):

  inbox    intake folder the watcher consumes
  library  ``*.library`` directory

Environment overrides (highest priority): ``EAGLE_INBOX``, ``EAGLE_LIBRARY``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

# Bootstrap only — used to *find* the vault config, not as the inbox path.
_BOOTSTRAP_VAULT = Path.home() / "Dropbox/ISAAC/GENNIE"
_BOOTSTRAP_LIBRARY = _BOOTSTRAP_VAULT / "Eunbi.library"


@dataclass(frozen=True)
class Settings:
    inbox: Path
    library: Path
    config_files: tuple[Path, ...]


def _read_toml(path: Path) -> dict[str, object]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _as_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _resolve(value: str, origin: Path) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = (origin.parent / p).resolve()
    return p


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    lib_hint = Path(
        os.environ.get("EAGLE_LIBRARY", str(_BOOTSTRAP_LIBRARY))
    ).expanduser()
    files.append(lib_hint.parent / "eagle-browse.toml")
    files.append(Path.home() / ".config/eagle-browse/config.toml")
    explicit = os.environ.get("EAGLE_BROWSE_CONFIG")
    if explicit:
        files.append(Path(explicit).expanduser())
    return files


def load_settings() -> Settings:
    inbox_val: str | None = None
    inbox_origin: Path | None = None
    library_val: str | None = None
    library_origin: Path | None = None
    used: list[Path] = []

    for path in _candidate_files():
        if not path.is_file():
            continue
        data = _read_toml(path)
        used.append(path)
        if (v := _as_str(data.get("inbox"))) is not None:
            inbox_val, inbox_origin = v, path
        if (v := _as_str(data.get("library"))) is not None:
            library_val, library_origin = v, path

    env_inbox = os.environ.get("EAGLE_INBOX")
    if env_inbox:
        inbox = Path(env_inbox).expanduser()
    elif inbox_val and inbox_origin is not None:
        inbox = _resolve(inbox_val, inbox_origin)
    else:
        inbox = _BOOTSTRAP_VAULT / "intake"

    env_library = os.environ.get("EAGLE_LIBRARY")
    if env_library:
        library = Path(env_library).expanduser()
    elif library_val and library_origin is not None:
        library = _resolve(library_val, library_origin)
    else:
        library = _BOOTSTRAP_LIBRARY

    return Settings(
        inbox=inbox.expanduser(),
        library=library.expanduser(),
        config_files=tuple(used),
    )


def inbox_path() -> Path:
    return load_settings().inbox


def library_path() -> Path:
    return load_settings().library


# Import-time aliases for existing callers (argparse defaults, etc.)
DEFAULT_INBOX = inbox_path()
DEFAULT_LIBRARY = library_path()

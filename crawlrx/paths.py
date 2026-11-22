"""paths.py -- where CrawlRX keeps caches, logs and health files.

Everything lives under one writable directory:

* ``CRAWLRX_DATA_DIR`` if set (the Docker image uses ``/data``)
* else ``%LOCALAPPDATA%\\crawlrx`` on Windows
* else ``$XDG_DATA_HOME/crawlrx`` (default ``~/.local/share/crawlrx``)

Individual files can still be overridden by their own ``CRAWLRX_*`` env var.
"""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    override = os.getenv("CRAWLRX_DATA_DIR", "").strip()
    if override:
        root = Path(override).expanduser()
    elif os.name == "nt":
        root = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "crawlrx"
    else:
        root = Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "crawlrx"
    root.mkdir(parents=True, exist_ok=True)
    return root


def data_path(*parts: str) -> str:
    """Absolute path (as ``str``) to a file under :func:`data_dir`."""
    path = data_dir().joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def env_path(env_name: str, *default_parts: str) -> str:
    """File path: ``$env_name`` if set, else ``data_path(*default_parts)``."""
    value = os.getenv(env_name, "").strip()
    return value or data_path(*default_parts)


def env_dir(env_name: str, *default_parts: str) -> str:
    """Directory path (created): ``$env_name`` if set, else under :func:`data_dir`."""
    value = os.getenv(env_name, "").strip()
    path = Path(value).expanduser() if value else data_dir().joinpath(*default_parts)
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


__all__ = ["data_dir", "data_path", "env_dir", "env_path"]

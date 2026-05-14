"""Application settings from ``config/app_settings.json`` (optional) and environment variables."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

from sp_sync.paths import project_root

DEFAULTS: dict[str, Any] = {
    # Relative to project root, or absolute path
    "database_path": "data/app.sqlite",
    # Optional default guest/share link used before REST (Playwright seed)
    "default_guest_share_link": "",
}


@lru_cache(maxsize=1)
def load_settings() -> dict[str, Any]:
    root = project_root()
    merged = dict(DEFAULTS)
    cfg = os.path.join(root, "config", "app_settings.json")
    if os.path.isfile(cfg):
        try:
            with open(cfg, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in DEFAULTS or k in ("database_path", "default_guest_share_link"):
                        merged[k] = v
        except (OSError, json.JSONDecodeError):
            pass

    db_env = os.environ.get("SP_SYNC_DB_PATH", "").strip()
    if db_env:
        merged["database_path"] = db_env
    guest_env = os.environ.get("SP_GUEST_LINK", "").strip()
    if guest_env:
        merged["default_guest_share_link"] = guest_env

    return merged


def clear_settings_cache() -> None:
    load_settings.cache_clear()


def database_file_path() -> str:
    """Resolved absolute path to the SQLite database file."""
    raw = (load_settings().get("database_path") or "data/app.sqlite").strip()
    root = project_root()
    path = raw if os.path.isabs(raw) else os.path.join(root, raw)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return os.path.abspath(path)


def default_guest_share_link() -> str:
    return (load_settings().get("default_guest_share_link") or "").strip()

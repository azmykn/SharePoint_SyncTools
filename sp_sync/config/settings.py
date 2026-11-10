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
    # Post-download duration check: fail when |actual-expected| > max(abs, pct*expected)
    "duration_tolerance_pct": 0.02,
    "duration_tolerance_seconds": 30.0,
    # Post-download silence trim (start/end only via longest merged speech region)
    "trim_silence_enabled": False,
    "trim_silence_noise_db": -70.0,
    "trim_silence_min_duration": 2.0,
    "trim_silence_margin_sec": 3.0,
    "trim_silence_merge_gap_sec": 120.0,
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
                    if k in DEFAULTS:
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


def duration_tolerance() -> tuple[float, float]:
    """Return ``(pct, abs_seconds)`` for post-download duration verification."""
    s = load_settings()
    try:
        pct = float(s.get("duration_tolerance_pct", 0.02))
    except (TypeError, ValueError):
        pct = 0.02
    try:
        abs_sec = float(s.get("duration_tolerance_seconds", 30.0))
    except (TypeError, ValueError):
        abs_sec = 30.0
    return max(0.0, pct), max(0.0, abs_sec)

"""Application settings from ``config/app_settings.json``, environment variables, and SQLite UI overrides."""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any

from sp_sync.paths import project_root

_logger = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    # Relative to project root, or absolute path
    "database_path": "data/app.sqlite",
    # Optional default guest/share link used before REST (Playwright seed)
    "default_guest_share_link": "",
    # Post-download duration check: fail when |actual-expected| > max(abs, pct*expected)
    "duration_tolerance_pct": 0.02,
    "duration_tolerance_seconds": 30.0,
    # Post-download silence trim (start/end only via first-to-last speech)
    "trim_silence_enabled": False,
    "trim_silence_noise_db": -70.0,
    "trim_silence_min_duration": 2.0,
    "trim_silence_margin_sec": 3.0,
    "trim_silence_merge_gap_sec": 120.0,
    "trim_silence_keep_backup": True,
}

EDITABLE_SETTINGS_KEYS = (
    "duration_tolerance_pct",
    "duration_tolerance_seconds",
    "trim_silence_enabled",
    "trim_silence_noise_db",
    "trim_silence_min_duration",
    "trim_silence_margin_sec",
    "trim_silence_merge_gap_sec",
    "trim_silence_keep_backup",
)


@lru_cache(maxsize=1)
def _load_file_and_env_settings() -> dict[str, Any]:
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
        except (OSError, json.JSONDecodeError) as exc:
            _logger.warning("Failed to parse %s (using defaults): %s", cfg, exc)

    db_env = os.environ.get("SP_SYNC_DB_PATH", "").strip()
    if db_env:
        merged["database_path"] = db_env
    guest_env = os.environ.get("SP_GUEST_LINK", "").strip()
    if guest_env:
        merged["default_guest_share_link"] = guest_env

    return merged


def load_settings(*, include_db_overrides: bool = True) -> dict[str, Any]:
    merged = dict(_load_file_and_env_settings())
    if include_db_overrides:
        try:
            from sp_sync.db.store import get_store

            overrides = get_store().get_app_settings_overrides()
            if isinstance(overrides, dict):
                for k in EDITABLE_SETTINGS_KEYS:
                    if k in overrides:
                        merged[k] = overrides[k]
        except Exception:
            pass
    return merged


def clear_settings_cache() -> None:
    _load_file_and_env_settings.cache_clear()


def database_file_path() -> str:
    """Resolved absolute path to the SQLite database file."""
    raw = (_load_file_and_env_settings().get("database_path") or "data/app.sqlite").strip()
    root = project_root()
    path = raw if os.path.isabs(raw) else os.path.join(root, raw)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    return os.path.abspath(path)


def default_guest_share_link() -> str:
    return (_load_file_and_env_settings().get("default_guest_share_link") or "").strip()


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


def get_editable_app_settings() -> dict[str, Any]:
    """Return current user-editable processing settings for the Web UI."""
    s = load_settings(include_db_overrides=True)
    return {k: s.get(k, DEFAULTS[k]) for k in EDITABLE_SETTINGS_KEYS}


def validate_and_save_app_settings(updates: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist editable settings into SQLite."""
    from sp_sync.db.store import get_store

    current = get_editable_app_settings()

    def _to_bool(v: Any, key: str) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            low = v.strip().lower()
            if low in ("1", "true", "yes", "on"):
                return True
            if low in ("0", "false", "no", "off", ""):
                return False
        raise ValueError(f"'{key}' must be a boolean")

    def _to_float(v: Any, key: str, lo: float, hi: float) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"'{key}' must be a number")
        if f < lo or f > hi:
            raise ValueError(f"'{key}' must be between {lo} and {hi}")
        return f

    if "duration_tolerance_pct" in updates:
        current["duration_tolerance_pct"] = _to_float(updates["duration_tolerance_pct"], "duration_tolerance_pct", 0.0, 0.5)
    if "duration_tolerance_seconds" in updates:
        current["duration_tolerance_seconds"] = _to_float(updates["duration_tolerance_seconds"], "duration_tolerance_seconds", 0.0, 600.0)
    if "trim_silence_enabled" in updates:
        current["trim_silence_enabled"] = _to_bool(updates["trim_silence_enabled"], "trim_silence_enabled")
    if "trim_silence_keep_backup" in updates:
        current["trim_silence_keep_backup"] = _to_bool(updates["trim_silence_keep_backup"], "trim_silence_keep_backup")
    if "trim_silence_noise_db" in updates:
        current["trim_silence_noise_db"] = _to_float(updates["trim_silence_noise_db"], "trim_silence_noise_db", -100.0, -10.0)
    if "trim_silence_min_duration" in updates:
        current["trim_silence_min_duration"] = _to_float(updates["trim_silence_min_duration"], "trim_silence_min_duration", 0.5, 60.0)
    if "trim_silence_margin_sec" in updates:
        current["trim_silence_margin_sec"] = _to_float(updates["trim_silence_margin_sec"], "trim_silence_margin_sec", 0.0, 60.0)
    if "trim_silence_merge_gap_sec" in updates:
        current["trim_silence_merge_gap_sec"] = _to_float(updates["trim_silence_merge_gap_sec"], "trim_silence_merge_gap_sec", 0.0, 1800.0)

    get_store().set_app_settings_overrides(current)
    return current

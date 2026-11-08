"""
Background Scheduled Sync Runner
================================
Runs unattended sync jobs at configured intervals (e.g. every 2, 6, 12, or 24 hours).
Persists configuration in SQLite and prevents overlapping runs.
"""

import time
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, Callable, Optional

from sp_sync.db.store import get_store

_logger = logging.getLogger(__name__)

KEY_SCHEDULER_SETTINGS = "scheduler_settings"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": False,
    "interval_hours": 6,
    "target": "all",              # "all", "sharepoint", "gdrive"
    "last_run": None,
    "next_run": None,
}

_daemon_thread: Optional[threading.Thread] = None
_sync_callback: Optional[Callable[[str], None]] = None
_lock = threading.RLock()
_running_event = threading.Event()


def get_scheduler_settings() -> Dict[str, Any]:
    """Load scheduler settings from SQLite store."""
    store = get_store()
    raw = store.get_json(KEY_SCHEDULER_SETTINGS, None)
    if isinstance(raw, dict):
        merged = dict(DEFAULT_SETTINGS)
        merged.update(raw)
        return merged
    return dict(DEFAULT_SETTINGS)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off", ""}:
            return False
    raise ValueError("'enabled' must be a boolean value")


def save_scheduler_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Save scheduler settings to SQLite store and recompute next_run.

    Validation rules:
    - enabled: boolean-like
    - interval_hours: one of {1, 2, 4, 6, 12, 24}
    - target: one of {'all', 'sharepoint', 'gdrive'}
    """
    current = get_scheduler_settings()

    if "enabled" in settings:
        current["enabled"] = _to_bool(settings.get("enabled"))

    if "interval_hours" in settings:
        raw_interval = settings.get("interval_hours")
        if raw_interval is None:
            raise ValueError("'interval_hours' must be an integer")
        try:
            interval = int(raw_interval)
        except (TypeError, ValueError):
            raise ValueError("'interval_hours' must be an integer")
        allowed = {1, 2, 4, 6, 12, 24}
        if interval not in allowed:
            raise ValueError("'interval_hours' must be one of: 1, 2, 4, 6, 12, 24")
        current["interval_hours"] = interval

    if "target" in settings:
        target = str(settings.get("target") or "").strip().lower()
        if target not in {"all", "sharepoint", "gdrive"}:
            raise ValueError("'target' must be one of: all, sharepoint, gdrive")
        current["target"] = target

    if "last_run" in settings:
        last_run = settings.get("last_run")
        if last_run in (None, ""):
            current["last_run"] = None
        elif isinstance(last_run, str):
            try:
                datetime.strptime(last_run, "%Y-%m-%d %H:%M:%S")
            except Exception:
                raise ValueError("'last_run' must be in format YYYY-MM-DD HH:MM:SS")
            current["last_run"] = last_run
        else:
            raise ValueError("'last_run' must be a string timestamp or null")

    # Recompute next_run timestamp
    if current.get("enabled"):
        interval = int(current.get("interval_hours", 6))
        last_str = current.get("last_run")
        if last_str:
            try:
                last_dt = datetime.strptime(last_str, "%Y-%m-%d %H:%M:%S")
                next_dt = last_dt + timedelta(hours=interval)
                current["next_run"] = next_dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                current["next_run"] = (datetime.now() + timedelta(hours=interval)).strftime("%Y-%m-%d %H:%M:%S")
        else:
            current["next_run"] = (datetime.now() + timedelta(hours=interval)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        current["next_run"] = None

    get_store().set_json(KEY_SCHEDULER_SETTINGS, current)
    return current


def mark_scheduler_run_completed(target: str = "all") -> None:
    """Record completion timestamp and schedule next execution."""
    now = datetime.now()
    cfg = get_scheduler_settings()
    cfg["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
    if cfg.get("enabled"):
        interval = max(1, int(cfg.get("interval_hours", 6)))
        cfg["next_run"] = (now + timedelta(hours=interval)).strftime("%Y-%m-%d %H:%M:%S")
    get_store().set_json(KEY_SCHEDULER_SETTINGS, cfg)


def is_run_due() -> bool:
    """Check if scheduled sync is currently due to execute."""
    cfg = get_scheduler_settings()
    if not cfg.get("enabled"):
        return False

    last_str = cfg.get("last_run")
    if not last_str:
        return True

    try:
        last_dt = datetime.strptime(last_str, "%Y-%m-%d %H:%M:%S")
        interval = max(1, int(cfg.get("interval_hours", 6)))
        return datetime.now() >= last_dt + timedelta(hours=interval)
    except Exception as exc:
        _logger.warning("Error parsing scheduler last_run: %s", exc)
        return True


def _scheduler_loop():
    """Background polling loop that checks for due jobs every 20 seconds."""
    while _running_event.is_set():
        try:
            if is_run_due():
                cfg = get_scheduler_settings()
                target = cfg.get("target", "all")
                _logger.info("Scheduler triggered automatic sync (target=%s)", target)
                if _sync_callback:
                    _sync_callback(target)
                mark_scheduler_run_completed(target)
        except Exception as exc:
            _logger.exception("Error in scheduler loop: %s", exc)

        # Sleep in small slices to allow rapid shutdown
        for _ in range(20):
            if not _running_event.is_set():
                break
            time.sleep(1)


def start_scheduler_daemon(sync_callback: Callable[[str], None]) -> None:
    """Start the background scheduler daemon thread if not already running."""
    global _daemon_thread, _sync_callback
    with _lock:
        _sync_callback = sync_callback
        if _daemon_thread and _daemon_thread.is_alive():
            return
        _running_event.set()
        _daemon_thread = threading.Thread(
            target=_scheduler_loop,
            daemon=True,
            name="SyncSchedulerDaemon",
        )
        _daemon_thread.start()
        _logger.info("Sync scheduler daemon started.")


def is_scheduler_daemon_alive() -> bool:
    """Whether the scheduler daemon thread is alive."""
    t = _daemon_thread
    return bool(t and t.is_alive())


def run_scheduler_now(target: str | None = None) -> dict[str, Any]:
    """Trigger scheduler callback immediately (manual force-run).

    Returns a small status payload for API/UI usage.
    """
    cfg = get_scheduler_settings()
    tgt = (target or cfg.get("target") or "all").strip().lower()
    if tgt not in {"all", "sharepoint", "gdrive"}:
        raise ValueError("target must be one of: all, sharepoint, gdrive")

    if _sync_callback is None:
        raise RuntimeError("Scheduler callback is not initialized")

    _sync_callback(tgt)
    mark_scheduler_run_completed(tgt)
    updated = get_scheduler_settings()
    return {
        "success": True,
        "target": tgt,
        "last_run": updated.get("last_run"),
        "next_run": updated.get("next_run"),
    }


def stop_scheduler_daemon() -> None:
    """Stop the background scheduler daemon thread."""
    _running_event.clear()

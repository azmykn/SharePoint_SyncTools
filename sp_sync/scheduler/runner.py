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
KEY_SCHEDULER_SUMMARY = "scheduler_last_summary"

DEFAULT_SETTINGS: Dict[str, Any] = {
    "enabled": False,
    "schedule_mode": "daily",     # "daily" | "interval"
    "daily_time": "02:00",        # local HH:MM for daily mode
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


def _parse_daily_time(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        hh, mm = raw.split(":", 1)
        hour = int(hh)
        minute = int(mm)
    except (TypeError, ValueError):
        raise ValueError("'daily_time' must be HH:MM (24h)")
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("'daily_time' must be HH:MM (24h)")
    return f"{hour:02d}:{minute:02d}"


def _next_daily_run(now: datetime, daily_time: str, last_run: Optional[str] = None) -> datetime:
    hour, minute = (int(x) for x in daily_time.split(":", 1))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    if last_run:
        try:
            last_dt = datetime.strptime(last_run, "%Y-%m-%d %H:%M:%S")
            # If we already ran after today's scheduled slot, push to tomorrow's slot.
            todays_slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if last_dt >= todays_slot and candidate.date() == now.date():
                candidate = todays_slot + timedelta(days=1)
        except Exception:
            pass
    return candidate


def _compute_next_run(cfg: Dict[str, Any], now: Optional[datetime] = None) -> Optional[str]:
    if not cfg.get("enabled"):
        return None
    now = now or datetime.now()
    mode = str(cfg.get("schedule_mode") or "daily").strip().lower()
    if mode == "interval":
        interval = max(1, int(cfg.get("interval_hours", 6)))
        last_str = cfg.get("last_run")
        if last_str:
            try:
                last_dt = datetime.strptime(last_str, "%Y-%m-%d %H:%M:%S")
                return (last_dt + timedelta(hours=interval)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        return (now + timedelta(hours=interval)).strftime("%Y-%m-%d %H:%M:%S")
    daily_time = _parse_daily_time(cfg.get("daily_time") or "02:00")
    return _next_daily_run(now, daily_time, cfg.get("last_run")).strftime("%Y-%m-%d %H:%M:%S")


def save_scheduler_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Save scheduler settings to SQLite store and recompute next_run.

    Validation rules:
    - enabled: boolean-like
    - schedule_mode: 'daily' | 'interval'
    - daily_time: HH:MM (24h) when mode is daily
    - interval_hours: one of {1, 2, 4, 6, 12, 24}
    - target: one of {'all', 'sharepoint', 'gdrive'}
    """
    current = get_scheduler_settings()

    if "enabled" in settings:
        current["enabled"] = _to_bool(settings.get("enabled"))

    if "schedule_mode" in settings:
        mode = str(settings.get("schedule_mode") or "").strip().lower()
        if mode not in {"daily", "interval"}:
            raise ValueError("'schedule_mode' must be one of: daily, interval")
        current["schedule_mode"] = mode

    if "daily_time" in settings:
        current["daily_time"] = _parse_daily_time(settings.get("daily_time"))

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

    # Ensure defaults for older stored configs
    if not current.get("schedule_mode"):
        current["schedule_mode"] = "daily"
    if not current.get("daily_time"):
        current["daily_time"] = "02:00"

    current["next_run"] = _compute_next_run(current)
    get_store().set_json(KEY_SCHEDULER_SETTINGS, current)
    return current


def mark_scheduler_run_completed(target: str = "all") -> None:
    """Record completion timestamp and schedule next execution."""
    now = datetime.now()
    cfg = get_scheduler_settings()
    cfg["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
    cfg["next_run"] = _compute_next_run(cfg, now=now)
    get_store().set_json(KEY_SCHEDULER_SETTINGS, cfg)


def is_run_due() -> bool:
    """Check if scheduled sync is currently due to execute."""
    cfg = get_scheduler_settings()
    if not cfg.get("enabled"):
        return False

    mode = str(cfg.get("schedule_mode") or "daily").strip().lower()
    last_str = cfg.get("last_run")
    now = datetime.now()

    if mode == "interval":
        if not last_str:
            return True
        try:
            last_dt = datetime.strptime(last_str, "%Y-%m-%d %H:%M:%S")
            interval = max(1, int(cfg.get("interval_hours", 6)))
            return now >= last_dt + timedelta(hours=interval)
        except Exception as exc:
            _logger.warning("Error parsing scheduler last_run: %s", exc)
            return True

    # Daily mode: due once the clock passes today's daily_time, if not already run since then.
    try:
        daily_time = _parse_daily_time(cfg.get("daily_time") or "02:00")
    except ValueError:
        daily_time = "02:00"
    hour, minute = (int(x) for x in daily_time.split(":", 1))
    todays_slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < todays_slot:
        return False
    if not last_str:
        return True
    try:
        last_dt = datetime.strptime(last_str, "%Y-%m-%d %H:%M:%S")
    except Exception as exc:
        _logger.warning("Error parsing scheduler last_run: %s", exc)
        return True
    return last_dt < todays_slot


def save_scheduler_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Persist last scheduled-run summary for UI/API."""
    payload = dict(summary or {})
    get_store().set_json(KEY_SCHEDULER_SUMMARY, payload)
    return payload


def get_scheduler_summary() -> Dict[str, Any]:
    raw = get_store().get_json(KEY_SCHEDULER_SUMMARY, None)
    return dict(raw) if isinstance(raw, dict) else {}


def build_download_summary_line(summary: Dict[str, Any]) -> str:
    """Human-readable one-liner for logs."""
    files = summary.get("new_files") or []
    count = int(summary.get("downloaded_files") or len(files) or 0)
    if count <= 0:
        return "Scheduled sync summary: no new lectures downloaded."
    names = ", ".join(str(f) for f in files[:8])
    extra = f" (+{count - 8} more)" if count > 8 else ""
    if names:
        return f"Scheduled sync summary: {count} new lecture(s): {names}{extra}"
    return f"Scheduled sync summary: {count} new lecture(s)."


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

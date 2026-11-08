"""
Central Real-Time Progress & Metrics Tracker
============================================
Thread-safe tracker for live sync progress, download speeds, and queue counts.
Used by SharePoint and Google Drive engines and consumed by the Web API.
"""

import time
import threading
from typing import Dict, Any, Optional

_lock = threading.RLock()

_state: Dict[str, Any] = {
    "is_syncing": False,
    "service": "",               # "sharepoint", "gdrive", "all"
    "current_folder": "",
    "current_file": "",
    "file_progress_pct": 0,
    "downloaded_bytes": 0,
    "total_bytes": 0,
    "speed_bps": 0,              # bytes per second
    "eta_seconds": 0,
    "queue_completed": 0,
    "queue_total": 0,
    "last_update_ts": 0.0,
    "status_message": "",
}

_last_sample_bytes = 0
_last_sample_time = 0.0


def set_sync_active(active: bool, service: str = "", queue_total: int = 0) -> None:
    """Mark sync as active or inactive and reset state."""
    global _last_sample_bytes, _last_sample_time
    with _lock:
        _state["is_syncing"] = active
        _state["service"] = service if active else ""
        _state["queue_total"] = queue_total if active else 0
        _state["queue_completed"] = 0
        _state["file_progress_pct"] = 0
        _state["downloaded_bytes"] = 0
        _state["total_bytes"] = 0
        _state["speed_bps"] = 0
        _state["eta_seconds"] = 0
        _state["current_file"] = ""
        _state["current_folder"] = ""
        _state["status_message"] = "Starting sync..." if active else "Idle"
        _state["last_update_ts"] = time.time()
        _last_sample_bytes = 0
        _last_sample_time = time.time()


def start_file_progress(folder_name: str, file_name: str, total_bytes: int = 0) -> None:
    """Notify tracker that a new file download has started."""
    global _last_sample_bytes, _last_sample_time
    now = time.time()
    with _lock:
        _state["current_folder"] = folder_name
        _state["current_file"] = file_name
        _state["total_bytes"] = total_bytes
        _state["downloaded_bytes"] = 0
        _state["file_progress_pct"] = 0
        _state["speed_bps"] = 0
        _state["eta_seconds"] = 0
        _state["status_message"] = f"Downloading {file_name}"
        _state["last_update_ts"] = now
        _last_sample_bytes = 0
        _last_sample_time = now


def update_file_progress(downloaded_bytes: int, total_bytes: Optional[int] = None) -> None:
    """Update byte counts and compute download speed and ETA."""
    global _last_sample_bytes, _last_sample_time
    now = time.time()
    with _lock:
        if total_bytes is not None and total_bytes > 0:
            _state["total_bytes"] = total_bytes

        tot = _state["total_bytes"]
        _state["downloaded_bytes"] = downloaded_bytes
        if tot > 0:
            pct = min(100, int((downloaded_bytes / tot) * 100))
            _state["file_progress_pct"] = pct
        else:
            pct = 0

        dt = now - _last_sample_time
        if dt >= 0.8:
            db = downloaded_bytes - _last_sample_bytes
            if db > 0 and dt > 0:
                cur_speed = int(db / dt)
                # Smoothed speed: 70% current + 30% previous
                prev_speed = _state.get("speed_bps", 0)
                _state["speed_bps"] = int(0.7 * cur_speed + 0.3 * prev_speed) if prev_speed else cur_speed
                if tot > downloaded_bytes and _state["speed_bps"] > 0:
                    rem_bytes = tot - downloaded_bytes
                    _state["eta_seconds"] = int(rem_bytes / _state["speed_bps"])
                else:
                    _state["eta_seconds"] = 0
            _last_sample_bytes = downloaded_bytes
            _last_sample_time = now

        _state["last_update_ts"] = now


def complete_file_progress(file_name: str = "") -> None:
    """Mark current file as finished and increment completed count."""
    with _lock:
        _state["queue_completed"] += 1
        _state["file_progress_pct"] = 100
        _state["status_message"] = f"Finished {file_name or _state['current_file']}"
        _state["last_update_ts"] = time.time()


def reset_metrics() -> None:
    """Reset all metrics state to default idle values."""
    set_sync_active(False)


def get_live_metrics() -> Dict[str, Any]:
    """Return a snapshot of current live metrics formatted for JSON response."""
    with _lock:
        s = dict(_state)

    # Format human-readable strings
    speed_mbps = round(s["speed_bps"] / (1024 * 1024), 2) if s["speed_bps"] > 0 else 0.0
    downloaded_mb = round(s["downloaded_bytes"] / (1024 * 1024), 1)
    total_mb = round(s["total_bytes"] / (1024 * 1024), 1) if s["total_bytes"] > 0 else 0.0

    return {
        "is_syncing": s["is_syncing"],
        "is_active": s["is_syncing"],
        "service": s["service"],
        "current_folder": s["current_folder"],
        "current_file": s["current_file"],
        "file_progress_pct": s["file_progress_pct"],
        "percent": s["file_progress_pct"],
        "downloaded_bytes": s["downloaded_bytes"],
        "bytes_transferred": s["downloaded_bytes"],
        "total_bytes": s["total_bytes"],
        "downloaded_mb": downloaded_mb,
        "total_mb": total_mb,
        "speed_mbps": speed_mbps,
        "speed_mb_s": speed_mbps,
        "eta_seconds": s["eta_seconds"],
        "queue_completed": s["queue_completed"],
        "files_completed": s["queue_completed"],
        "queue_total": s["queue_total"],
        "total_files": s["queue_total"],
        "status_message": s["status_message"],
    }

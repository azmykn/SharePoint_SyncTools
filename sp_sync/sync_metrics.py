"""
Central Real-Time Progress & Metrics Tracker
============================================
Thread-safe tracker for live sync progress, download speeds, and queue counts.
Supports independent concurrent tracking for SharePoint and Google Drive while
maintaining full backward compatibility with the single-state snapshot API.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

_lock = threading.RLock()


def _blank_state(service: str = "") -> Dict[str, Any]:
    return {
        "is_syncing": False,
        "stopping": False,
        "service": service,
        "current_folder": "",
        "current_file": "",
        "file_progress_pct": 0,
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "speed_bps": 0,
        "eta_seconds": 0,
        "queue_completed": 0,
        "queue_total": 0,
        "last_update_ts": 0.0,
        "status_message": "Idle",
        "_last_sample_bytes": 0,
        "_last_sample_time": 0.0,
    }


# Kept as module-level dict for backward compatibility with tests inspecting _state directly.
_state: Dict[str, Any] = _blank_state("")
_service_states: Dict[str, Dict[str, Any]] = {
    "sharepoint": _blank_state("sharepoint"),
    "gdrive": _blank_state("gdrive"),
}
_active_default_service: str = ""


def _to_num(value: Any) -> float:
    """Coerce a byte/speed value (int, float, numeric str, None) to a number; 0 if invalid."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value
    try:
        num = float(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return int(num) if num.is_integer() else num


def _norm_service(service: Optional[str]) -> str:
    s = (service or "").strip().lower()
    if s in ("sharepoint", "gdrive"):
        return s
    return ""


def _target_bucket(service: Optional[str]) -> Optional[Dict[str, Any]]:
    svc = _norm_service(service)
    if svc:
        return _service_states[svc]
    if _active_default_service in _service_states:
        return _service_states[_active_default_service]
    return None


def _sync_aggregate_unlocked() -> None:
    """Refresh legacy top-level ``_state`` from active service buckets."""
    active = [s for s in _service_states.values() if s["is_syncing"]]
    if not active:
        _state["is_syncing"] = False
        _state["stopping"] = False
        _state["service"] = ""
        _state["status_message"] = "Idle"
        _state["speed_bps"] = 0
        _state["eta_seconds"] = 0
        return

    # Choose the most recently updated active service for primary file display
    primary = max(active, key=lambda s: float(s.get("last_update_ts") or 0.0))
    _state["is_syncing"] = True
    _state["stopping"] = all(bool(s.get("stopping")) for s in active)
    _state["service"] = "all" if len(active) > 1 else primary["service"]
    _state["current_folder"] = primary["current_folder"]
    _state["current_file"] = primary["current_file"]
    _state["file_progress_pct"] = primary["file_progress_pct"]
    _state["downloaded_bytes"] = primary["downloaded_bytes"]
    _state["total_bytes"] = primary["total_bytes"]
    _state["speed_bps"] = sum(_to_num(s.get("speed_bps")) for s in active)
    _state["eta_seconds"] = primary["eta_seconds"]
    _state["queue_completed"] = sum(int(s.get("queue_completed") or 0) for s in active)
    _state["queue_total"] = sum(int(s.get("queue_total") or 0) for s in active)
    _state["last_update_ts"] = max(float(s.get("last_update_ts") or 0.0) for s in active)
    _state["status_message"] = primary["status_message"]


def set_sync_active(active: bool, service: str = "", queue_total: int = 0) -> None:
    """Mark sync as active or inactive for a service (or all when service is empty)."""
    global _active_default_service
    now = time.time()
    svc = _norm_service(service)
    with _lock:
        if svc:
            bucket = _service_states[svc]
            bucket.update(_blank_state(svc))
            bucket["is_syncing"] = bool(active)
            bucket["queue_total"] = int(queue_total) if active else 0
            bucket["status_message"] = "Starting sync..." if active else "Idle"
            bucket["last_update_ts"] = now
            bucket["_last_sample_time"] = now
            if active:
                _active_default_service = svc
            elif _active_default_service == svc:
                other = next((k for k, v in _service_states.items() if v["is_syncing"]), "")
                _active_default_service = other
            _sync_aggregate_unlocked()
        else:
            if not active:
                for k in list(_service_states):
                    _service_states[k] = _blank_state(k)
                    _service_states[k]["last_update_ts"] = now
                _active_default_service = ""
            _state.update(_blank_state(service if active else ""))
            _state["is_syncing"] = bool(active)
            _state["queue_total"] = int(queue_total) if active else 0
            _state["status_message"] = "Starting sync..." if active else "Idle"
            _state["last_update_ts"] = now
            _state["_last_sample_time"] = now


def set_sync_stopping(service: str = "") -> None:
    """Mark a running sync as stopping (awaiting cancellation)."""
    svc = _norm_service(service)
    now = time.time()
    with _lock:
        targets = [_service_states[svc]] if svc else [s for s in _service_states.values() if s["is_syncing"]]
        for b in targets:
            if b["is_syncing"]:
                b["stopping"] = True
                b["status_message"] = "Stopping sync..."
                b["last_update_ts"] = now
        _sync_aggregate_unlocked()


def set_sync_status_message(message: str, *, service: Optional[str] = None) -> None:
    """Update human-readable status message for the active sync."""
    now = time.time()
    with _lock:
        b = _target_bucket(service)
        if b is not None:
            b["status_message"] = str(message)
            b["last_update_ts"] = now
            _sync_aggregate_unlocked()
        else:
            _state["status_message"] = str(message)
            _state["last_update_ts"] = now


def start_file_progress(
    folder_name: str, file_name: str, total_bytes: int = 0, *, service: Optional[str] = None
) -> None:
    """Notify tracker that a new file download has started."""
    now = time.time()
    tot = _to_num(total_bytes)
    with _lock:
        b = _target_bucket(service)
        target = b if b is not None else _state
        target["current_folder"] = folder_name
        target["current_file"] = file_name
        target["total_bytes"] = tot
        target["downloaded_bytes"] = 0
        target["file_progress_pct"] = 0
        target["speed_bps"] = 0
        target["eta_seconds"] = 0
        target["status_message"] = f"Downloading {file_name}"
        target["last_update_ts"] = now
        target["_last_sample_bytes"] = 0
        target["_last_sample_time"] = now
        if b is not None:
            _sync_aggregate_unlocked()


def update_file_progress(
    downloaded_bytes: Any,
    total_bytes: Optional[Any] = None,
    *,
    service: Optional[str] = None,
    percent_override: Optional[int] = None,
) -> None:
    """Update byte counts and compute smoothed download speed (with stall decay) and ETA."""
    dl = _to_num(downloaded_bytes)
    tot_arg = _to_num(total_bytes) if total_bytes is not None else None
    now = time.time()
    with _lock:
        b = _target_bucket(service)
        target = b if b is not None else _state
        if tot_arg is not None and tot_arg > 0:
            target["total_bytes"] = tot_arg

        tot = _to_num(target.get("total_bytes"))
        target["downloaded_bytes"] = dl
        if percent_override is not None:
            target["file_progress_pct"] = max(0, min(100, int(percent_override)))
        elif tot > 0:
            target["file_progress_pct"] = min(100, int((dl / tot) * 100))
        else:
            target["file_progress_pct"] = 0

        dt = now - float(target.get("_last_sample_time") or 0.0)
        if dt >= 0.8:
            prev_bytes = _to_num(target.get("_last_sample_bytes"))
            db = dl - prev_bytes
            if db > 0 and dt > 0:
                cur_speed = int(db / dt)
                prev_speed = _to_num(target.get("speed_bps"))
                target["speed_bps"] = int(0.7 * cur_speed + 0.3 * prev_speed) if prev_speed else cur_speed
                if tot > dl and target["speed_bps"] > 0:
                    target["eta_seconds"] = int((tot - dl) / target["speed_bps"])
                else:
                    target["eta_seconds"] = 0
            elif dt >= 3.0:
                # Decay speed cleanly to 0 when stalled instead of showing frozen stale speed.
                target["speed_bps"] = 0
                target["eta_seconds"] = 0
            target["_last_sample_bytes"] = dl
            target["_last_sample_time"] = now

        target["last_update_ts"] = now
        if b is not None:
            _sync_aggregate_unlocked()


def complete_file_progress(file_name: str = "", *, service: Optional[str] = None) -> None:
    """Mark current file as finished and increment completed count."""
    now = time.time()
    with _lock:
        b = _target_bucket(service)
        target = b if b is not None else _state
        target["queue_completed"] = int(target.get("queue_completed") or 0) + 1
        target["file_progress_pct"] = 100
        target["status_message"] = f"Finished {file_name or target.get('current_file') or ''}".strip()
        target["last_update_ts"] = now
        if b is not None:
            _sync_aggregate_unlocked()


def reset_metrics() -> None:
    """Reset all metrics state to default idle values."""
    set_sync_active(False)


def _format_snapshot(s: Dict[str, Any], now: float) -> Dict[str, Any]:
    speed_bps = _to_num(s.get("speed_bps"))
    dl_bytes = _to_num(s.get("downloaded_bytes"))
    tot_bytes = _to_num(s.get("total_bytes"))
    last_ts = float(s.get("last_update_ts") or 0.0)
    stale_sec = round(max(0.0, now - last_ts), 1) if last_ts > 0 else 0.0

    # Decay speed in snapshot too if no update arrived for > 5 seconds while syncing
    if s.get("is_syncing") and last_ts > 0 and (now - last_ts) > 5.0:
        speed_bps = 0

    speed_mbps = round(speed_bps / (1024 * 1024), 2) if speed_bps > 0 else 0.0
    downloaded_mb = round(dl_bytes / (1024 * 1024), 1)
    total_mb = round(tot_bytes / (1024 * 1024), 1) if tot_bytes > 0 else 0.0

    if not s.get("is_syncing"):
        status_state = "idle"
    elif s.get("stopping"):
        status_state = "stopping"
    elif last_ts > 0 and (now - last_ts) > 20.0:
        status_state = "stalling"
    else:
        status_state = "running"

    return {
        "is_syncing": bool(s.get("is_syncing")),
        "is_active": bool(s.get("is_syncing")),
        "stopping": bool(s.get("stopping")),
        "status_state": status_state,
        "service": s.get("service") or "",
        "current_folder": s.get("current_folder") or "",
        "current_file": s.get("current_file") or "",
        "file_progress_pct": int(s.get("file_progress_pct") or 0),
        "percent": int(s.get("file_progress_pct") or 0),
        "downloaded_bytes": dl_bytes,
        "bytes_transferred": dl_bytes,
        "total_bytes": tot_bytes,
        "downloaded_mb": downloaded_mb,
        "total_mb": total_mb,
        "speed_mbps": speed_mbps,
        "speed_mb_s": speed_mbps,
        "eta_seconds": int(s.get("eta_seconds") or 0),
        "queue_completed": int(s.get("queue_completed") or 0),
        "files_completed": int(s.get("queue_completed") or 0),
        "queue_total": int(s.get("queue_total") or 0),
        "total_files": int(s.get("queue_total") or 0),
        "status_message": s.get("status_message") or "",
        "last_update_ts": last_ts,
        "stale_seconds": stale_sec,
    }


def get_live_metrics() -> Dict[str, Any]:
    """Return a snapshot of current live metrics formatted for JSON response."""
    now = time.time()
    with _lock:
        top = dict(_state)
        services_copy = {k: dict(v) for k, v in _service_states.items()}

    out = _format_snapshot(top, now)
    out["services"] = {k: _format_snapshot(v, now) for k, v in services_copy.items()}
    return out

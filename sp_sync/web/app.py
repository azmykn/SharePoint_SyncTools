"""
Unified Sync Tool - Flask Web App Factory & Wiring
===================================================
Modular Flask application registering SharePoint, Google Drive, Sync, and System blueprints,
guarded by local-origin CSRF protection and unified SyncOrchestrator.
"""

from __future__ import annotations

import io
import os
import sys
import threading
import urllib.parse
from datetime import datetime
from typing import List

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

from flask import Flask, request

from sp_sync.paths import project_root
from sp_sync.scheduler import (
    build_download_summary_line,
    get_scheduler_settings,
    get_scheduler_summary,
    is_run_due,
    is_scheduler_daemon_alive,
    run_scheduler_now,
    save_scheduler_settings,
    save_scheduler_summary,
    start_scheduler_daemon,
)
from sp_sync.sync_orchestrator import SyncOrchestrator
from sp_sync.web.helpers import (
    _json_error,
    _ledger_groups,
    _normalize_config_results,
    _normalize_sp_bulk_item,
    _normalize_sp_rel_key,
    _prepare_sync_report,
    _request_json,
    _save_sync_report,
    _sp_sync_enabled,
    add_log,
    detect_sharepoint_site_root_from_url,
    hydrate_logs_from_db,
    load_gdrive_configs,
    load_sp_configs,
    load_sp_cookies,
    log_buffer,
    save_gdrive_configs,
    save_sp_configs,
    save_sp_cookies,
    sp_site,
    validate_sharepoint_site_url_for_save,
    validate_sp_cookies,
)
from sp_sync.web.routes_gd import gd_bp
from sp_sync.web.routes_sp import sp_bp
from sp_sync.web.routes_sync import sync_bp
from sp_sync.web.routes_system import system_bp

ROOT = project_root()

_orchestrator = SyncOrchestrator(log_fn=add_log, save_report_fn=_save_sync_report)
# Backward-compatible reference to live sync_status dict
sync_status = _orchestrator.status


def _publish_scheduler_summary(target: str, new_files: list, downloaded_files: int, errors: list) -> None:
    summary = {
        "target": target,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "downloaded_files": int(downloaded_files or 0),
        "new_files": list(new_files or [])[:200],
        "errors": list(errors or [])[:20],
    }
    save_scheduler_summary(summary)
    line = build_download_summary_line(summary)
    add_log("system", f"📋 {line}")
    try:
        from sp_sync.notifications.telegram import notify_sync_summary

        notify_sync_summary(summary)
    except Exception:
        pass


def _trigger_scheduled_sync(target: str = "all") -> None:
    """Callback invoked by background scheduler daemon using the unified SyncOrchestrator."""
    try:
        add_log("system", f"⏰ Scheduled auto-sync starting (target={target})...")
        summary_files: List[str] = []
        summary_errors: List[str] = []
        summary_total = {"n": 0}
        summary_lock = threading.Lock()
        pending_threads: List[threading.Thread] = []

        def _collect(report: dict) -> None:
            with summary_lock:
                summary_files.extend(report.get("new_files") or [])
                summary_errors.extend(report.get("errors") or [])
                summary_total["n"] += int(report.get("downloaded_files") or 0)

        if target in ("all", "sharepoint") and not _orchestrator.is_running("sharepoint"):
            fedauth, rtfa = load_sp_cookies()
            site = sp_site()
            if fedauth and site:
                sp_configs = load_sp_configs()
                ok, _msg, t_sp = _orchestrator.start_sharepoint(
                    fedauth=fedauth,
                    rtfa=rtfa,
                    site_url=site,
                    configs=sp_configs,
                    trigger="scheduler",
                    on_complete=_collect,
                )
                if ok and t_sp is not None:
                    pending_threads.append(t_sp)

        if target in ("all", "gdrive") and not _orchestrator.is_running("gdrive"):
            from sp_sync.gdrive import is_authenticated

            if is_authenticated():
                gd_configs = load_gdrive_configs()
                if gd_configs:
                    ok, _msg, t_gd = _orchestrator.start_gdrive(
                        configs=gd_configs,
                        trigger="scheduler",
                        on_complete=_collect,
                    )
                    if ok and t_gd is not None:
                        pending_threads.append(t_gd)

        def _finalize_summary() -> None:
            for t in pending_threads:
                t.join(timeout=3600 * 6)
            with summary_lock:
                files_copy = list(summary_files)
                errors_copy = list(summary_errors)
                total_copy = int(summary_total["n"])
            _publish_scheduler_summary(target, files_copy, total_copy, errors_copy)

        if pending_threads:
            threading.Thread(target=_finalize_summary, daemon=True, name="SchedulerSummaryJoiner").start()
        else:
            _publish_scheduler_summary(target, [], 0, [])
    except Exception as exc:
        add_log("system", f"❌ Scheduled sync error: {exc}")


def ensure_scheduler_started() -> None:
    """Start background scheduler daemon explicitly (avoids side effects during test imports)."""
    try:
        start_scheduler_daemon(_trigger_scheduled_sync)
    except Exception:
        pass


def _is_local_origin(origin_or_referer: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(origin_or_referer)
        host = (parsed.hostname or "").lower()
        return host in ("127.0.0.1", "localhost", "::1")
    except Exception:
        return False


def create_app(*, start_scheduler: bool = False) -> Flask:
    """Flask app factory."""
    flask_app = Flask(
        __name__,
        template_folder=os.path.join(ROOT, "templates"),
        static_folder=os.path.join(ROOT, "static"),
        static_url_path="/static",
    )
    flask_app.secret_key = os.urandom(24)
    flask_app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB cap
    flask_app.extensions["sync_orchestrator"] = _orchestrator

    @flask_app.before_request
    def _csrf_local_origin_guard():
        if flask_app.config.get("TESTING"):
            return None
        if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
            return None
        origin = (request.headers.get("Origin") or "").strip()
        referer = (request.headers.get("Referer") or "").strip()
        if origin and not _is_local_origin(origin):
            return _json_error("Cross-origin write request blocked.", 403, code="CSRF_ORIGIN_BLOCKED")
        if not origin and referer and not _is_local_origin(referer):
            return _json_error("Cross-origin referer blocked.", 403, code="CSRF_REFERER_BLOCKED")
        return None

    flask_app.register_blueprint(system_bp)
    flask_app.register_blueprint(sync_bp)
    flask_app.register_blueprint(sp_bp)
    flask_app.register_blueprint(gd_bp)

    if start_scheduler:
        hydrate_logs_from_db()
        ensure_scheduler_started()

    return flask_app


# Default module-level Flask instance (no background thread launched during test imports)
app = create_app(start_scheduler=False)


if __name__ == "__main__":
    from sp_sync.single_instance import acquire_app_lock, release_app_lock

    host = os.environ.get("SP_SYNC_HOST", "127.0.0.1")
    port = int(os.environ.get("SP_SYNC_PORT", os.environ.get("PORT", "5000")))
    ok, msg = acquire_app_lock(host=host, port=port)
    if not ok:
        print(f"❌ {msg}")
        raise SystemExit(1)
    hydrate_logs_from_db()
    ensure_scheduler_started()
    print("=" * 50)
    print(f"  Unified Sync Tool — http://{host}:{port}")
    print(f"  {msg}")
    print("  Tip: run from repo root with: python app.py")
    print("=" * 50)
    try:
        app.run(host=host, port=port, debug=False)
    finally:
        release_app_lock()

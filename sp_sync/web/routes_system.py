"""Dashboard index, logs, scheduler, download ledger, app settings, health, and backup/restore routes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime

from flask import Blueprint, Response, current_app, jsonify, redirect, render_template, request, url_for

from sp_sync.config.settings import get_editable_app_settings, validate_and_save_app_settings
from sp_sync.scheduler import (
    get_scheduler_settings,
    get_scheduler_summary,
    is_run_due,
    is_scheduler_daemon_alive,
    save_scheduler_settings,
)
from sp_sync.web.helpers import (
    _json_error,
    _ledger_groups,
    _log_lock,
    _request_json,
    add_log,
    build_shell_context,
    cleanup_tmp_segments,
    get_system_health,
    is_allowed_open_folder_target,
    load_gdrive_configs,
    load_sp_configs,
    load_sp_cookies,
    log_buffer,
    save_gdrive_configs,
    save_sp_configs,
    sp_site,
)

system_bp = Blueprint("system_bp", __name__)

_LEGACY_TAB_REDIRECTS = {
    "overview": "system_bp.index",
    "sp": "system_bp.sharepoint_page",
    "sp-explore": "system_bp.explorer_page",
    "gd": "system_bp.gdrive_page",
    "ledger": "system_bp.ledger_page",
    "settings": "system_bp.settings_page",
    "logs": "system_bp.logs_page",
}


def _wants_html_page() -> bool:
    """Return True for normal browser page navigation (Accept includes text/html), False for JSON/API clients."""
    if request.args.get("format") == "json" or request.args.get("after_id"):
        return False
    if (request.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest":
        return False
    accept = (request.headers.get("Accept") or "").lower()
    if "text/html" not in accept:
        return False
    first_mime = accept.split(",")[0].strip()
    return not first_mime.startswith("application/json")


def _query_logs_json():
    """Shared JSON query logic for ``/api/logs`` and non-HTML ``/logs`` requests."""
    from sp_sync.db.store import get_store

    source = (request.args.get("source") or "all").strip().lower()
    level = (request.args.get("level") or "all").strip().lower()
    q = (request.args.get("q") or "").strip().lower()
    raw_after = (request.args.get("after_id") or "").strip()
    raw_limit = (request.args.get("limit") or "300").strip()
    try:
        limit = max(1, min(500, int(raw_limit)))
    except ValueError:
        limit = 300

    after_id = 0
    if raw_after:
        try:
            after_id = max(0, int(raw_after))
        except ValueError:
            after_id = 0

    try:
        entries = get_store().recent_logs(limit, source=source, after_id=after_id)
    except Exception:
        entries = []

    # Fallback to in-memory buffer only if SQLite returned nothing on initial load
    if not entries and after_id == 0:
        with _log_lock:
            entries = list(log_buffer)
        if source != "all":
            entries = [e for e in entries if e.get("source") == source]
        entries = entries[-limit:]

    if level != "all":
        filtered = []
        for e in entries:
            msg = str(e.get("msg") or "")
            low = msg.lower()
            if level == "error" and ("❌" in msg or "error" in low or "failed" in low):
                filtered.append(e)
            elif level == "warning" and ("⚠️" in msg or "🛑" in msg or "warn" in low):
                filtered.append(e)
            elif level == "success" and ("✅" in msg or "🎉" in msg):
                filtered.append(e)
        entries = filtered

    if q:
        entries = [
            e
            for e in entries
            if q in str(e.get("msg") or "").lower()
            or q in str(e.get("source") or "").lower()
            or q in str(e.get("time") or "").lower()
        ]

    return jsonify(entries[-limit:])


@system_bp.route("/")
def index():
    """Overview page (also redirects legacy ``/?tab=...`` links to their dedicated URLs)."""
    legacy_tab = (request.args.get("tab") or "").strip().lower()
    if legacy_tab and legacy_tab in _LEGACY_TAB_REDIRECTS and legacy_tab != "overview":
        return redirect(url_for(_LEGACY_TAB_REDIRECTS[legacy_tab]))

    return render_template(
        "pages/overview.html",
        **build_shell_context(
            "overview",
            "navOverview",
            scheduler_settings=get_scheduler_settings(),
        ),
    )


@system_bp.route("/sharepoint")
def sharepoint_page():
    """Unified SharePoint hub (Sync Targets + integrated Explorer sub-view)."""
    sp_configs = load_sp_configs()
    sp_path_hint = ""
    for c in sp_configs or []:
        if c.get("rel_url") and str(c.get("entry_type", "folder")).lower() != "file":
            sp_path_hint = c["rel_url"]
            break
    view = (request.args.get("view") or "targets").strip().lower()
    if view not in ("targets", "explorer"):
        view = "targets"

    return render_template(
        "pages/sharepoint.html",
        **build_shell_context(
            "sharepoint",
            "navSp",
            sp_folders=sp_configs,
            sp_path_hint=sp_path_hint,
            initial_view=view,
        ),
    )


@system_bp.route("/explorer")
def explorer_page():
    """Direct URL alias that opens the SharePoint hub directly on the Explorer sub-view."""
    sp_configs = load_sp_configs()
    sp_path_hint = ""
    for c in sp_configs or []:
        if c.get("rel_url") and str(c.get("entry_type", "folder")).lower() != "file":
            sp_path_hint = c["rel_url"]
            break
    return render_template(
        "pages/sharepoint.html",
        **build_shell_context(
            "sharepoint",
            "navSp",
            sp_folders=sp_configs,
            sp_path_hint=sp_path_hint,
            initial_view="explorer",
        ),
    )


@system_bp.route("/gdrive")
def gdrive_page():
    """Google Drive targets hub."""
    return render_template(
        "pages/gdrive.html",
        **build_shell_context(
            "gdrive",
            "navGd",
            gd_folders=load_gdrive_configs(),
        ),
    )


@system_bp.route("/settings")
def settings_page():
    """Unified Settings & Health page organized into 3 horizontal sections."""
    from sp_sync.db.store import get_store

    section = (request.args.get("section") or "connections").strip().lower()
    if section not in ("connections", "automation", "health"):
        section = "connections"

    return render_template(
        "pages/settings.html",
        **build_shell_context(
            "settings",
            "navSettings",
            initial_section=section,
            sp_auto_cookies_supported=sys.platform == "win32",
            sp_guest_link_display=get_store().get_guest_link(),
            scheduler_settings=get_scheduler_settings(),
            app_settings=get_editable_app_settings(),
        ),
    )


@system_bp.route("/activity")
def logs_page():
    """Dedicated Live Activity Log page (with shared header & sidebar)."""
    return render_template(
        "pages/logs.html",
        **build_shell_context("logs", "navLogs"),
    )


@system_bp.route("/api/logs")
def api_get_logs():
    """Always returns JSON logs (supports incremental ``after_id``, source, level, search, limit)."""
    return _query_logs_json()


@system_bp.route("/validate_configs", methods=["GET"])
def validate_configs():
    try:
        from sp_sync.config.validator import validate_and_fix_configs

        results = validate_and_fix_configs()
        return jsonify({"success": True, "results": results})
    except Exception as e:
        return _json_error(f"Validation error: {e}", 500, code="CONFIG_VALIDATION_ERROR")


@system_bp.route("/logs")
def get_logs():
    """Serve the Live Activity Log page to browsers, and JSON logs to API/test clients."""
    if _wants_html_page():
        return render_template(
            "pages/logs.html",
            **build_shell_context("logs", "navLogs"),
        )
    return _query_logs_json()


@system_bp.route("/logs/text")
def get_logs_text():
    source = (request.args.get("source") or "all").strip().lower()
    with _log_lock:
        entries = list(log_buffer)
    if source != "all":
        entries = [e for e in entries if e.get("source") == source]
    entries = entries[-400:]
    tag = {"sharepoint": "SP", "gdrive": "GD", "system": "SYS"}
    lines = []
    for e in entries:
        lab = tag.get(e.get("source", ""), str(e.get("source", "SYS"))[:3].upper())
        lines.append(f"{e.get('time', '')} [{lab}] {e.get('msg', '')}")
    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")


@system_bp.route("/logs/clear", methods=["POST"])
def clear_logs_route():
    data = _request_json()
    source = (data.get("source") or request.args.get("source") or "all").strip().lower()
    if source not in ("all", "sharepoint", "gdrive", "system"):
        return _json_error("Invalid source.", 400, code="INVALID_LOG_SOURCE")

    try:
        from sp_sync.db.store import get_store

        get_store().clear_logs(None if source == "all" else source)
    except Exception as e:
        return _json_error(str(e), 500, code="LOG_CLEAR_FAILED")

    with _log_lock:
        if source == "all":
            log_buffer.clear()
        else:
            kept = [e for e in log_buffer if e.get("source") != source]
            log_buffer.clear()
            log_buffer.extend(kept)

    label = "all sources" if source == "all" else source
    add_log("system", f"🧹 Log cleared ({label})")
    return jsonify({"success": True, "source": source})


@system_bp.route("/api/browse_folder")
def api_browse_folder():
    """Open native folder chooser safely inside an isolated subprocess (never runs Tk in Flask thread)."""
    if sys.platform != "win32" and sys.platform != "darwin":
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return jsonify({"success": False, "error": "No graphical display available for native folder dialog."}), 200

    script = (
        "import tkinter as tk; from tkinter import filedialog; "
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True); "
        "p = filedialog.askdirectory(); r.destroy(); print(p or '')"
    )
    try:
        res = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        folder_selected = (res.stdout or "").strip()
        if res.returncode == 0 and folder_selected:
            return jsonify({"success": True, "path": os.path.normpath(folder_selected)})
        if res.returncode != 0:
            err = (res.stderr or "").strip().splitlines()[-1:] or ["Native dialog unavailable"]
            return jsonify({"success": False, "error": err[0]}), 200
        return jsonify({"success": False})
    except Exception as e:
        return jsonify({"success": False, "error": f"Native folder dialog unavailable: {e}"}), 200


@system_bp.route("/api/open_local_folder", methods=["POST"])
def api_open_local_folder():
    """Open an existing local directory (or configured sync root) in the OS file manager."""
    data = _request_json()
    folder_path = (data.get("folder_path") or data.get("path") or "").strip()
    if not folder_path:
        return _json_error("No folder path provided.", 400, code="MISSING_FOLDER_PATH")

    ok, resolved_or_err = is_allowed_open_folder_target(folder_path)
    if not ok:
        return _json_error(resolved_or_err, 400, code="INVALID_FOLDER_PATH")

    try:
        if sys.platform == "win32":
            os.startfile(resolved_or_err)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", resolved_or_err], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["xdg-open", resolved_or_err], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"success": True, "path": resolved_or_err})
    except Exception as exc:
        return _json_error(str(exc), 500, code="OPEN_FOLDER_FAILED")


@system_bp.route("/scheduler/settings", methods=["GET", "POST"])
def scheduler_settings_route():
    if request.method == "POST":
        data = _request_json()
        try:
            updated = save_scheduler_settings(data)
        except ValueError as exc:
            return _json_error(str(exc), 400, code="INVALID_SCHEDULER_SETTINGS")
        except Exception as exc:
            return _json_error(str(exc), 500, code="SCHEDULER_SETTINGS_SAVE_FAILED")

        state_str = "enabled" if updated.get("enabled") else "disabled"
        mode = updated.get("schedule_mode") or "daily"
        when = f"daily at {updated.get('daily_time')}" if mode == "daily" else f"every {updated.get('interval_hours')} hours"
        add_log("system", f"⏰ Scheduler {state_str}: {when} (target={updated.get('target')})")
        return jsonify(
            {
                "success": True,
                "settings": updated,
                "is_due": is_run_due(),
                "daemon_alive": is_scheduler_daemon_alive(),
                "summary": get_scheduler_summary(),
            }
        )

    current = get_scheduler_settings()
    return jsonify(
        {
            "success": True,
            "settings": current,
            "is_due": is_run_due(),
            "daemon_alive": is_scheduler_daemon_alive(),
            "summary": get_scheduler_summary(),
        }
    )


@system_bp.route("/scheduler/summary")
def scheduler_summary_route():
    return jsonify(
        {
            "success": True,
            "summary": get_scheduler_summary(),
            "settings": get_scheduler_settings(),
        }
    )


@system_bp.route("/scheduler/run_now", methods=["POST"])
def scheduler_run_now_route():
    import sp_sync.web.app as web_app_mod

    data = _request_json()
    target = (data.get("target") or "").strip().lower() or None
    try:
        result = web_app_mod.run_scheduler_now(target)
    except ValueError as exc:
        return _json_error(str(exc), 400, code="INVALID_SCHEDULER_TARGET")
    except RuntimeError as exc:
        return _json_error(str(exc), 503, code="SCHEDULER_NOT_READY")
    except Exception as exc:
        return _json_error(str(exc), 500, code="SCHEDULER_RUN_NOW_FAILED")

    add_log("system", f"▶️ Scheduler force-run requested (target={result.get('target')})")
    return jsonify(result)


@system_bp.route("/ledger")
def ledger_page():
    """Unified Downloaded Lectures Ledger page (with shared header & sidebar)."""
    groups = _ledger_groups()
    total_items = sum(len(g["items"]) for g in groups)
    total_mb = round(sum(float(g.get("total_mb") or 0) for g in groups), 1)
    missing = sum(1 for g in groups for it in g["items"] if not it.get("local_exists"))
    pending = sum(1 for g in groups for it in g["items"] if it.get("redownload_requested"))
    stats = {
        "subjects": len(groups),
        "total_items": total_items,
        "total_mb": total_mb,
        "missing_count": missing,
        "pending_redownload": pending,
    }
    return render_template(
        "pages/ledger.html",
        **build_shell_context("ledger", "navLedger", groups=groups, ledger_stats=stats),
    )


@system_bp.route("/api/ledger")
def api_ledger():
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "all").strip().lower()
    groups = _ledger_groups(query=q, status_filter=status)
    total_items = sum(len(g["items"]) for g in groups)
    total_mb = round(sum(float(g.get("total_mb") or 0) for g in groups), 1)
    missing = sum(1 for g in groups for it in g["items"] if not it.get("local_exists"))
    pending = sum(1 for g in groups for it in g["items"] if it.get("redownload_requested"))
    return jsonify(
        {
            "success": True,
            "groups": groups,
            "stats": {
                "subjects": len(groups),
                "total_items": total_items,
                "total_mb": total_mb,
                "missing_count": missing,
                "pending_redownload": pending,
            },
        }
    )


@system_bp.route("/api/ledger/redownload", methods=["POST"])
def api_ledger_redownload():
    from sp_sync.db.store import get_store

    data = _request_json()
    key = (data.get("item_key") or "").strip()
    if not key:
        return _json_error("item_key is required", 400, code="MISSING_ITEM_KEY")
    cancel = bool(data.get("cancel"))
    store = get_store()
    ok = store.ledger_cancel_redownload(key) if cancel else store.ledger_request_redownload(key)
    if not ok:
        return _json_error("Ledger entry not found", 404, code="LEDGER_ENTRY_NOT_FOUND")
    add_log("system", f"{'↩️ Re-download cancelled' if cancel else '🔁 Re-download requested'}: {key}")
    return jsonify({"success": True, "item_key": key, "redownload_requested": not cancel})


@system_bp.route("/api/ledger/delete", methods=["POST"])
def api_ledger_delete():
    from sp_sync.db.store import get_store

    data = _request_json()
    key = (data.get("item_key") or "").strip()
    if not key:
        return _json_error("item_key is required", 400, code="MISSING_ITEM_KEY")
    ok = get_store().ledger_delete(key)
    if not ok:
        return _json_error("Ledger entry not found", 404, code="LEDGER_ENTRY_NOT_FOUND")
    add_log("system", f"🗑️ Ledger entry deleted: {key}")
    return jsonify({"success": True, "item_key": key})


@system_bp.route("/api/app_settings", methods=["GET", "POST"])
def api_app_settings():
    if request.method == "POST":
        data = _request_json()
        try:
            updated = validate_and_save_app_settings(data)
        except ValueError as exc:
            return _json_error(str(exc), 400, code="INVALID_APP_SETTINGS")
        except Exception as exc:
            return _json_error(str(exc), 500, code="APP_SETTINGS_SAVE_FAILED")
        add_log("system", "⚙️ Application processing settings updated.")
        return jsonify({"success": True, "settings": updated})
    return jsonify({"success": True, "settings": get_editable_app_settings()})


@system_bp.route("/api/system/health", methods=["GET"])
def api_system_health():
    return jsonify({"success": True, "health": get_system_health()})


@system_bp.route("/api/system/cleanup_temp", methods=["POST"])
def api_cleanup_temp():
    data = _request_json()
    try:
        min_age = float(data.get("min_age_hours", 0))
    except (TypeError, ValueError):
        return _json_error("min_age_hours must be a number", 400, code="INVALID_AGE")
    empty_only = bool(data.get("empty_only", False))
    orch = current_app.extensions["sync_orchestrator"]
    if orch.is_running("sharepoint") and not empty_only and min_age < 1:
        return _json_error(
            "Cannot purge active temporary segments while SharePoint sync is running.",
            409,
            code="SYNC_RUNNING",
        )
    res = cleanup_tmp_segments(min_age_hours=min_age, active_only_empty=empty_only)
    add_log(
        "system",
        f"🧹 Temp cleanup: removed {res['removed_dirs']} dir(s), {res['removed_files']} file(s), freed {res['freed_mb']} MB",
    )
    return jsonify({"success": True, **res})


@system_bp.route("/api/config/export", methods=["GET"])
def api_config_export():
    """Export non-secret configs (SharePoint folders, GDrive folders, scheduler, app settings)."""
    payload = {
        "schema_version": 1,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sharepoint_site_url": sp_site(),
        "sharepoint_configs": load_sp_configs(),
        "gdrive_configs": load_gdrive_configs(),
        "scheduler_settings": get_scheduler_settings(),
        "app_settings": get_editable_app_settings(),
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    fname = f"sync_config_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{fname}"',
    }
    return Response(body, headers=headers)


@system_bp.route("/api/config/import", methods=["POST"])
def api_config_import():
    """Import exported JSON backup (non-secret folder configs, scheduler, app settings)."""
    from sp_sync.db.store import get_store
    from sp_sync.web.helpers import validate_sharepoint_site_url_for_save

    data = _request_json()
    if not data:
        return _json_error("JSON backup payload is required.", 400, code="EMPTY_BACKUP")

    imported = {}
    if isinstance(data.get("sharepoint_site_url"), str) and data["sharepoint_site_url"].strip():
        ok, _err = validate_sharepoint_site_url_for_save(data["sharepoint_site_url"])
        if ok:
            get_store().set_sharepoint_site_url(data["sharepoint_site_url"].strip().rstrip("/"))
            imported["sharepoint_site_url"] = True

    if isinstance(data.get("sharepoint_configs"), list):
        saved_sp = save_sp_configs(data["sharepoint_configs"])
        imported["sharepoint_configs"] = len(saved_sp)

    if isinstance(data.get("gdrive_configs"), list):
        saved_gd = save_gdrive_configs(data["gdrive_configs"])
        imported["gdrive_configs"] = len(saved_gd)

    if isinstance(data.get("scheduler_settings"), dict):
        sched = {
            k: v
            for k, v in data["scheduler_settings"].items()
            if k in ("enabled", "schedule_mode", "daily_time", "interval_hours", "target")
        }
        if sched:
            save_scheduler_settings(sched)
            imported["scheduler_settings"] = True

    if isinstance(data.get("app_settings"), dict):
        validate_and_save_app_settings(data["app_settings"])
        imported["app_settings"] = True

    if not imported:
        return _json_error("Backup contained no recognized settings keys.", 400, code="INVALID_BACKUP")

    add_log("system", f"📥 Imported configuration backup: {imported}")
    return jsonify({"success": True, "imported": imported})

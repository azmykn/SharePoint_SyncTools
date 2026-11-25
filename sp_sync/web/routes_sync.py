"""Sync execution, cancellation, live metrics, and sync report/history routes."""

from __future__ import annotations

import json
from datetime import datetime

from flask import Blueprint, Response, current_app, jsonify, request

from sp_sync.sync_metrics import get_live_metrics
from sp_sync.web.helpers import (
    _json_error,
    _request_json,
    add_log,
    load_gdrive_configs,
    load_sp_configs,
    load_sp_cookies,
    sp_site,
)

sync_bp = Blueprint("sync_bp", __name__)


def _orch():
    return current_app.extensions["sync_orchestrator"]


@sync_bp.route("/sp/sync", methods=["POST"])
def sp_sync():
    data = _request_json()
    dry_run = bool(data.get("dry_run", False))
    raw_index = data.get("index", None)
    config_index = None
    if raw_index is not None and str(raw_index).strip() != "":
        try:
            config_index = int(raw_index)
        except (TypeError, ValueError):
            return _json_error("Invalid index.", 400, code="INVALID_INDEX")

    orch = _orch()
    if orch.is_running("sharepoint"):
        return _json_error("A SharePoint sync is already running.", 409, code="SYNC_ALREADY_RUNNING")

    fedauth, rtfa = load_sp_cookies()
    if not fedauth:
        return _json_error("Please save FedAuth first.", 401, code="MISSING_COOKIES")
    site = sp_site()
    if not site:
        return _json_error(
            "SharePoint site URL is not set yet. Paste a full SharePoint link once in "
            "SharePoint explorer (auto-detect), or set it under Connection settings.",
            400,
            code="MISSING_SITE_URL",
        )

    sp_configs = load_sp_configs()
    if not sp_configs:
        return _json_error("No SharePoint folders configured.", 400, code="NO_SP_CONFIGS")

    ok, msg, _t = orch.start_sharepoint(
        fedauth=fedauth,
        rtfa=rtfa,
        site_url=site,
        configs=sp_configs,
        trigger="single_folder" if config_index is not None else ("dry_run" if dry_run else "manual"),
        dry_run=dry_run,
        config_index=config_index,
    )
    if not ok:
        return _json_error(msg, 409 if "running" in msg.lower() else 400, code="SP_SYNC_START_FAILED")
    return jsonify({"success": True, "message": msg, "dry_run": dry_run, "index": config_index})


@sync_bp.route("/gdrive/sync", methods=["POST"])
def gdrive_sync():
    data = _request_json()
    dry_run = bool(data.get("dry_run", False))
    raw_index = data.get("index", None)
    config_index = None
    if raw_index is not None and str(raw_index).strip() != "":
        try:
            config_index = int(raw_index)
        except (TypeError, ValueError):
            return _json_error("Invalid index.", 400, code="INVALID_INDEX")

    orch = _orch()
    if orch.is_running("gdrive"):
        return _json_error("A Google Drive sync is already running.", 409, code="SYNC_ALREADY_RUNNING")

    from sp_sync.gdrive import is_authenticated

    if not is_authenticated():
        return _json_error("Please sign in to Google Drive first.", 401, code="GDRIVE_NOT_AUTHENTICATED")

    configs = load_gdrive_configs()
    if not configs:
        return _json_error("No Google Drive folders configured.", 400, code="NO_GDRIVE_CONFIGS")

    ok, msg, _t = orch.start_gdrive(
        configs=configs,
        trigger="single_folder" if config_index is not None else ("dry_run" if dry_run else "manual"),
        dry_run=dry_run,
        config_index=config_index,
    )
    if not ok:
        return _json_error(msg, 409 if "running" in msg.lower() else 400, code="GDRIVE_SYNC_START_FAILED")
    return jsonify({"success": True, "message": msg, "dry_run": dry_run, "index": config_index})


@sync_bp.route("/sync/all", methods=["POST"])
def sync_all():
    """Sync both SharePoint and Google Drive concurrently via the unified orchestrator."""
    data = _request_json()
    dry_run = bool(data.get("dry_run", False))
    orch = _orch()
    started = {}

    if not orch.is_running("sharepoint"):
        fedauth, rtfa = load_sp_cookies()
        site = sp_site()
        sp_configs = load_sp_configs()
        if fedauth and site and sp_configs:
            ok, _msg, _t = orch.start_sharepoint(
                fedauth=fedauth,
                rtfa=rtfa,
                site_url=site,
                configs=sp_configs,
                trigger="dry_run" if dry_run else "sync_all",
                dry_run=dry_run,
            )
            if ok:
                started["sharepoint"] = "started"
        elif fedauth and not site:
            add_log("sharepoint", "⚠️ Cannot start SharePoint sync: site URL is not configured.")

    if not orch.is_running("gdrive"):
        from sp_sync.gdrive import is_authenticated

        gd_configs = load_gdrive_configs()
        if gd_configs and is_authenticated():
            ok, _msg, _t = orch.start_gdrive(
                configs=gd_configs,
                trigger="dry_run" if dry_run else "sync_all",
                dry_run=dry_run,
            )
            if ok:
                started["gdrive"] = "started"

    if not started:
        return _json_error(
            "No sync started. Check credentials, configured folders, and that no sync is already running.",
            400,
            code="NO_SYNC_STARTED",
        )
    return jsonify({"success": True, "started": started, "dry_run": dry_run})


@sync_bp.route("/sync/stop", methods=["POST"])
def sync_stop():
    """Request graceful stop of a running sync ('sharepoint', 'gdrive', or 'all')."""
    data = _request_json()
    service = (data.get("service") or "all").strip().lower()
    if service not in ("all", "sharepoint", "gdrive"):
        return _json_error("service must be one of: all, sharepoint, gdrive", 400, code="INVALID_SERVICE")
    res = _orch().request_stop(service)
    return jsonify(res)


@sync_bp.route("/status")
def get_status():
    return jsonify(_orch().status)


@sync_bp.route("/sync/live_metrics")
def sync_live_metrics():
    return jsonify(get_live_metrics())


@sync_bp.route("/sync/report")
def sync_report():
    from sp_sync.db.store import get_store

    service = (request.args.get("service") or "all").strip().lower()
    if service not in ("all", "sharepoint", "gdrive"):
        return _json_error("service must be one of: all, sharepoint, gdrive", 400, code="INVALID_SERVICE")

    store = get_store()
    if service == "all":
        return jsonify(
            {
                "success": True,
                "reports": {
                    "sharepoint": store.get_sync_report("sharepoint"),
                    "gdrive": store.get_sync_report("gdrive"),
                },
            }
        )
    return jsonify({"success": True, "report": store.get_sync_report(service), "service": service})


@sync_bp.route("/sync/report/clear", methods=["POST"])
def sync_report_clear():
    from sp_sync.db.store import get_store

    data = _request_json()
    service = (data.get("service") or request.args.get("service") or "all").strip().lower()
    if service not in ("all", "sharepoint", "gdrive"):
        return _json_error("service must be one of: all, sharepoint, gdrive", 400, code="INVALID_SERVICE")

    store = get_store()
    if service in ("all", "sharepoint"):
        store.set_sync_report("sharepoint", {})
    if service in ("all", "gdrive"):
        store.set_sync_report("gdrive", {})
    store.clear_sync_report_history(service)

    add_log("system", f"🧹 Sync report cleared ({service})")
    return jsonify({"success": True, "service": service})


@sync_bp.route("/sync/report/history")
def sync_report_history():
    from sp_sync.db.store import get_store

    service = (request.args.get("service") or "all").strip().lower()
    if service not in ("all", "sharepoint", "gdrive"):
        return _json_error("service must be one of: all, sharepoint, gdrive", 400, code="INVALID_SERVICE")

    raw_limit = (request.args.get("limit") or "20").strip()
    try:
        limit = int(raw_limit)
    except ValueError:
        return _json_error("limit must be an integer", 400, code="INVALID_LIMIT")
    if limit < 1 or limit > 200:
        return _json_error("limit must be between 1 and 200", 400, code="INVALID_LIMIT")

    q = (request.args.get("q") or "").strip()
    items = get_store().get_sync_report_history(service=service, limit=limit, query=q)
    return jsonify({"success": True, "service": service, "limit": limit, "q": q, "items": items})


@sync_bp.route("/sync/report/history/export")
def sync_report_history_export():
    from sp_sync.db.store import get_store

    service = (request.args.get("service") or "all").strip().lower()
    if service not in ("all", "sharepoint", "gdrive"):
        return _json_error("service must be one of: all, sharepoint, gdrive", 400, code="INVALID_SERVICE")

    raw_limit = (request.args.get("limit") or "50").strip()
    try:
        limit = int(raw_limit)
    except ValueError:
        return _json_error("limit must be an integer", 400, code="INVALID_LIMIT")
    if limit < 1 or limit > 500:
        return _json_error("limit must be between 1 and 500", 400, code="INVALID_LIMIT")

    q = (request.args.get("q") or "").strip()
    items = get_store().get_sync_report_history(service=service, limit=limit, query=q)
    payload = {
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "service": service,
        "limit": limit,
        "q": q,
        "count": len(items),
        "items": items,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    fname = f"sync_report_history_{service}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{fname}"',
    }
    return Response(body, headers=headers)

"""Google Drive OAuth, credentials upload, and folder configuration routes."""

from __future__ import annotations

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from sp_sync.web.helpers import (
    _gd_sync_enabled,
    _json_error,
    _request_json,
    add_log,
    build_shell_context,
    load_gdrive_configs,
    save_gdrive_configs,
    validate_gdrive_config_row,
)

gd_bp = Blueprint("gd_bp", __name__)


@gd_bp.route("/gdrive/configs", methods=["GET"])
def gdrive_configs_get():
    return jsonify({"success": True, "configs": load_gdrive_configs()})


@gd_bp.route("/gdrive/auth")
def gdrive_auth():
    return redirect(url_for("gd_bp.gdrive_auth_page"))


@gd_bp.route("/gdrive/auth_page")
def gdrive_auth_page():
    from sp_sync.gdrive import get_cached_user_email, has_credentials, is_authenticated

    auth = is_authenticated(allow_network_refresh=False)
    user = get_cached_user_email() if auth else None
    return render_template(
        "pages/gdrive_auth.html",
        **build_shell_context(
            "gdrive",
            "gdAuthTitle",
            authenticated=auth,
            user=user,
            has_credentials=has_credentials(),
        ),
    )


@gd_bp.route("/gdrive/auth/start", methods=["POST"])
def gdrive_auth_start():
    from sp_sync.gdrive import start_oauth_flow

    try:
        res = start_oauth_flow(open_browser=True)
        add_log("gdrive", "🔐 Google Drive authentication started in browser...")
        return jsonify(res)
    except FileNotFoundError as exc:
        return _json_error(str(exc), 400, code="GDRIVE_CREDENTIALS_MISSING", need_credentials=True)
    except Exception as exc:
        return _json_error(str(exc), 500, code="GDRIVE_AUTH_START_FAILED")


@gd_bp.route("/gdrive/auth/status", methods=["GET"])
def gdrive_auth_status():
    from sp_sync.gdrive import get_oauth_status

    refresh = request.args.get("refresh", "").strip().lower() in ("1", "true", "yes")
    return jsonify(get_oauth_status(refresh_user=refresh))


@gd_bp.route("/gdrive/auth/code", methods=["POST"])
def gdrive_auth_code():
    data = _request_json()
    code = (data.get("code") or data.get("url") or "").strip()
    from sp_sync.gdrive import complete_oauth_with_code

    res = complete_oauth_with_code(code)
    if res.get("success"):
        add_log("gdrive", f"✅ Google Drive connected as {res.get('user_email')}")
        return jsonify(res)
    return _json_error(res.get("error") or "Code exchange failed", 400, code="GDRIVE_AUTH_CODE_FAILED")


@gd_bp.route("/gdrive/auth/disconnect", methods=["POST"])
def gdrive_auth_disconnect():
    from sp_sync.gdrive import disconnect_gdrive

    disconnect_gdrive()
    add_log("gdrive", "🚪 Google Drive disconnected.")
    return jsonify({"success": True})


@gd_bp.route("/gdrive/upload_credentials", methods=["POST"])
def gdrive_upload_credentials():
    from sp_sync.gdrive import save_credentials_content

    content = ""
    if "file" in request.files:
        f = request.files["file"]
        content = f.read().decode("utf-8", errors="replace")
    else:
        data = _request_json()
        content = (data.get("content") or "").strip()

    if not content:
        return _json_error("No file or content provided.", 400, code="EMPTY_CREDENTIALS")

    try:
        save_credentials_content(content)
        add_log("gdrive", "✅ Google credentials.json saved successfully.")
        return jsonify({"success": True})
    except Exception as exc:
        return _json_error(str(exc), 400, code="INVALID_CREDENTIALS_JSON")


@gd_bp.route("/gdrive/get_folder_info", methods=["POST"])
def gdrive_get_folder_info():
    data = _request_json()
    folder_url = (data.get("folder_url") or "").strip()

    from sp_sync.gdrive import extract_folder_id, get_credentials, get_folder_metadata

    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        return _json_error("Invalid URL.", 400, code="INVALID_GDRIVE_URL")

    creds = get_credentials()
    if not creds or not creds.valid:
        return _json_error("Please sign in first.", 401, code="GDRIVE_NOT_AUTHENTICATED")

    metadata = get_folder_metadata(creds, folder_id)
    if metadata:
        return jsonify({"success": True, "name": metadata.get("name"), "folder_id": folder_id})
    return _json_error("Folder not found.", 404, code="GDRIVE_FOLDER_NOT_FOUND")


@gd_bp.route("/gdrive/add_folder", methods=["POST"])
def gdrive_add_folder():
    data = _request_json()
    folder_url = (data.get("folder_url") or "").strip()
    local_path = (data.get("local_path") or "").strip()
    name = (data.get("name") or "").strip()

    if not folder_url or not local_path:
        return _json_error("Folder URL and local path are required.", 400, code="INVALID_GDRIVE_CONFIG")

    from sp_sync.gdrive import extract_folder_id

    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        return _json_error("Invalid Google Drive URL.", 400, code="INVALID_GDRIVE_URL")

    row = {
        "name": name or f"Google Drive Folder ({folder_id[:8]}...)",
        "folder_url": folder_url,
        "folder_id": folder_id,
        "local_path": local_path,
        "sync_enabled": data.get("sync_enabled", True) is not False,
    }
    normalized, val = validate_gdrive_config_row(row)
    if not val["valid"]:
        return _json_error("; ".join(val["errors"]), 400, code="INVALID_GDRIVE_CONFIG", errors=val["errors"])

    configs = load_gdrive_configs()
    configs.append(normalized)
    save_gdrive_configs(configs)
    add_log("gdrive", f"📁 Folder added: {normalized['name']}")
    return jsonify({"success": True, "config": normalized, "warnings": val.get("warnings", [])})


@gd_bp.route("/gdrive/update_folder", methods=["POST"])
def gdrive_update_folder():
    data = _request_json()
    try:
        index = int(data.get("index", -1))
    except (TypeError, ValueError):
        return _json_error("Invalid index.", 400, code="INVALID_INDEX")

    folder_url = (data.get("folder_url") or "").strip()
    local_path = (data.get("local_path") or "").strip()
    name = (data.get("name") or "").strip()

    configs = load_gdrive_configs()
    if not (0 <= index < len(configs)):
        return _json_error("Invalid index.", 400, code="INVALID_INDEX")

    from sp_sync.gdrive import extract_folder_id

    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        return _json_error("Invalid Google Drive URL.", 400, code="INVALID_GDRIVE_URL")

    prev = configs[index]
    row = {
        "name": name or f"Google Drive Folder ({folder_id[:8]}...)",
        "folder_url": folder_url,
        "folder_id": folder_id,
        "local_path": local_path,
        "sync_enabled": (
            data.get("sync_enabled") is not False
            if "sync_enabled" in data
            else _gd_sync_enabled(prev)
        ),
    }
    normalized, val = validate_gdrive_config_row(row)
    if not val["valid"]:
        return _json_error("; ".join(val["errors"]), 400, code="INVALID_GDRIVE_CONFIG", errors=val["errors"])

    configs[index] = normalized
    save_gdrive_configs(configs)
    add_log("gdrive", f"📝 Folder updated: {normalized['name']}")
    return jsonify({"success": True, "config": normalized, "warnings": val.get("warnings", [])})


@gd_bp.route("/gdrive/remove_folder", methods=["POST"])
def gdrive_remove_folder():
    data = _request_json()
    try:
        index = int(data.get("index", -1))
    except (TypeError, ValueError):
        return _json_error("Invalid index.", 400, code="INVALID_INDEX")

    configs = load_gdrive_configs()
    if 0 <= index < len(configs):
        removed = configs.pop(index)
        save_gdrive_configs(configs)
        add_log("gdrive", f"🗑️ Folder removed: {removed.get('name', 'unknown')}")
        return jsonify({"success": True})
    return _json_error("Invalid index.", 400, code="INVALID_INDEX")


@gd_bp.route("/gdrive/toggle_sync", methods=["POST"])
def gdrive_toggle_sync():
    data = _request_json()
    try:
        index = int(data.get("index", -1))
    except (TypeError, ValueError):
        return _json_error("Invalid index.", 400, code="INVALID_INDEX")

    configs = load_gdrive_configs()
    if not (0 <= index < len(configs)):
        return _json_error("Invalid index.", 400, code="INVALID_INDEX")

    enabled = data.get("enabled") is not False if "enabled" in data else (not _gd_sync_enabled(configs[index]))
    configs[index]["sync_enabled"] = enabled
    save_gdrive_configs(configs)
    name = configs[index].get("name", "Google Drive")
    add_log("gdrive", f"{'▶️ Sync enabled' if enabled else '⏸️ Sync paused'}: {name}")
    return jsonify({"success": True, "enabled": enabled, "index": index})


@gd_bp.route("/gdrive/reorder_folders", methods=["POST"])
def gdrive_reorder_folders():
    data = _request_json()
    order = data.get("order")
    current = load_gdrive_configs()
    if not isinstance(order, list) or len(order) != len(current):
        return _json_error("Invalid order array.", 400, code="INVALID_ORDER")
    try:
        indices = [int(i) for i in order]
    except (TypeError, ValueError):
        return _json_error("Order must be integer indices.", 400, code="INVALID_ORDER")
    if sorted(indices) != list(range(len(current))):
        return _json_error("Order must be a permutation of indices.", 400, code="INVALID_ORDER")
    reordered = [current[i] for i in indices]
    save_gdrive_configs(reordered)
    add_log("gdrive", "📋 Google Drive sync order updated")
    return jsonify({"success": True, "total": len(reordered)})

"""SharePoint configuration, cookie management, live testing, URL resolve, and REST browse routes."""

from __future__ import annotations

from flask import Blueprint, jsonify, redirect, url_for

from sp_sync.web.helpers import (
    _json_error,
    _normalize_sp_bulk_item,
    _normalize_sp_rel_key,
    _request_json,
    _sp_sync_enabled,
    add_log,
    detect_sharepoint_site_root_from_url,
    load_sp_configs,
    load_sp_cookies,
    save_sp_configs,
    save_sp_cookies,
    sp_site,
    validate_sharepoint_site_url_for_save,
    validate_sp_config_row,
    validate_sp_cookies,
)

sp_bp = Blueprint("sp_bp", __name__)


@sp_bp.route("/sp/configs", methods=["GET"])
def sp_configs_get():
    return jsonify({"success": True, "configs": load_sp_configs()})


@sp_bp.route("/sp/save_cookies", methods=["POST"])
def sp_save_cookies():
    data = _request_json()
    fedauth = (data.get("fedauth") or "").strip()
    rtfa = (data.get("rtfa") or "").strip()

    if not fedauth:
        return _json_error("FedAuth is required.", 400, code="MISSING_FEDAUTH")

    save_sp_cookies(fedauth, rtfa)
    add_log("sharepoint", "✅ Connection cookies saved.")
    return jsonify({"success": True, "fedauth_len": len(fedauth), "rtfa_len": len(rtfa)})


@sp_bp.route("/sp/save_site_url", methods=["POST"])
def sp_save_site_url():
    from sp_sync.db.store import get_store

    data = _request_json()
    raw = (data.get("site_url") or "").strip()
    ok, err = validate_sharepoint_site_url_for_save(raw)
    if not ok:
        return _json_error(err, 400, code="INVALID_SITE_URL")
    u = raw.strip().rstrip("/")
    get_store().set_sharepoint_site_url(u)
    add_log("system", f"SharePoint site URL saved: {u}")
    return jsonify({"success": True, "site_url": u})


@sp_bp.route("/sp/auto_cookies", methods=["POST"])
def sp_auto_cookies():
    try:
        from sp_sync.sharepoint.cookie_extractor import extract_sp_cookies

        result = extract_sp_cookies()
        if result["fedauth"]:
            save_sp_cookies(result["fedauth"], result["rtfa"])
            src = result.get("source") or "browser"
            add_log("sharepoint", f"✅ Cookies extracted from {src}")
            return jsonify({"success": True, "source": src})
        return _json_error(result.get("error") or "Extraction failed", 400, code="COOKIE_EXTRACT_FAILED")
    except Exception as e:
        return _json_error(f"Error: {e}", 500, code="COOKIE_EXTRACT_ERROR")


@sp_bp.route("/sp/validate_cookies", methods=["POST"])
def sp_validate_cookies():
    try:
        data = _request_json()
        fedauth = (data.get("fedauth") or "").strip()
        rtfa = (data.get("rtfa") or "").strip()

        if not fedauth or not rtfa:
            return _json_error("Both cookies are required for validation.", 400, code="MISSING_COOKIES")

        result = validate_sp_cookies(fedauth, rtfa)
        return jsonify({"success": True, "validation": result})
    except Exception as e:
        return _json_error(f"Validation error: {e}", 500, code="COOKIE_VALIDATION_ERROR")


@sp_bp.route("/sp/test_cookies", methods=["POST"])
def sp_test_cookies():
    """Live connectivity test against SharePoint /_api/web/currentuser using saved or supplied cookies."""
    from sp_sync.security.cookies import CookieValidator

    data = _request_json()
    fedauth = (data.get("fedauth") or "").strip()
    rtfa = (data.get("rtfa") or "").strip()
    if not fedauth:
        saved_fed, saved_rt = load_sp_cookies()
        fedauth = saved_fed
        if not rtfa:
            rtfa = saved_rt

    if not fedauth:
        return _json_error("No SharePoint cookies saved yet.", 400, code="MISSING_COOKIES")

    validator = CookieValidator()
    fmt = validator.validate_cookies(fedauth, rtfa)
    conn = validator.test_cookie_connectivity(fedauth, rtfa)
    return jsonify(
        {
            "success": bool(conn.get("success")),
            "format_validation": fmt,
            "connectivity": conn,
            "message": conn.get("message", ""),
            "user": conn.get("user", ""),
        }
    )


@sp_bp.route("/sp/browse", methods=["POST"])
def sp_browse():
    from sp_sync.db.store import get_store
    from sp_sync.sharepoint.rest_folder_list import browse_empty_hint_i18n, list_folder_children_rest
    from sp_sync.sharepoint.sync_engine import (
        _sp_share_requests_session,
        prime_sharepoint_session_for_browse,
    )

    fedauth, rtfa = load_sp_cookies()
    data = _request_json()
    prime_url = (data.get("prime_url") or "").strip()
    sharing_link_url = (data.get("sharing_link_url") or "").strip()
    if not sharing_link_url and prime_url and ":f:/" in prime_url.lower():
        sharing_link_url = prime_url
    for detect_u in (sharing_link_url, prime_url):
        if detect_u.lower().startswith("http"):
            detected = detect_sharepoint_site_root_from_url(detect_u)
            if detected:
                get_store().set_sharepoint_site_url(detected)
                break

    if not fedauth or not rtfa:
        return _json_error("Please save FedAuth and rtFa first.", 401, code="MISSING_COOKIES")
    if not sp_site():
        return _json_error(
            "SharePoint site URL is not set yet. Paste a full SharePoint link once in "
            "SharePoint explorer (auto-detect), or set it under Connection settings.",
            400,
            code="MISSING_SITE_URL",
        )

    parent_raw = (data.get("parent_rel_url") or "").strip()
    parent_item_id = (data.get("parent_item_id") or "").strip() or None
    share_browse_root = (data.get("share_browse_root") or "").strip() or None

    base = sp_site().rstrip("/")
    sess = _sp_share_requests_session(sp_site(), fedauth, rtfa, accept_html=False)
    try:
        for warm_url in (sharing_link_url, prime_url):
            if warm_url.lower().startswith("http"):
                prime_sharepoint_session_for_browse(
                    sess,
                    sp_site(),
                    prime_url=warm_url,
                    fedauth_cookie=fedauth,
                    rtfa_cookie=rtfa,
                )

        result = list_folder_children_rest(
            sess,
            base,
            parent_raw,
            timeout=60,
            sharing_link_url=sharing_link_url or None,
            parent_item_id=parent_item_id,
            share_root_norm=share_browse_root,
        )
    finally:
        sess.close()

    if not result.get("ok"):
        payload = {
            "success": False,
            "code": "SP_BROWSE_FAILED",
            "error": result.get("error") or "Browse failed",
            "parent": result.get("normalized_parent") or parent_raw,
            "method": result.get("method") or "",
            "request_url": (result.get("request_url") or "")[:300],
            "body_snippet": (result.get("body_snippet") or "")[:500].replace("\n", " "),
        }
        if result.get("content_type"):
            payload["content_type"] = result["content_type"]
        if result.get("upstream_http_status") is not None:
            payload["upstream_http_status"] = result["upstream_http_status"]
        if result.get("upstream_http_status") == 403:
            payload["permission_hint_en"] = (
                "SharePoint returned 403 (access denied) for this folder listing. "
                "Paste FedAuth and rtFa from the same browser profile where this link opens while you are signed in, "
                "then use Resolve link again."
            )
            payload["permission_hint_ar"] = (
                "رفض SharePoint عرض محتويات المجلد (403). "
                "الصق FedAuth و rtFa من نفس المتصفح والحساب الذي يفتح الرابط وهو مسجّل، ثم أعد حل الرابط."
            )
        http_out = result.get("upstream_http_status")
        if http_out in (401, 403, 404):
            return jsonify(payload), int(http_out)
        return jsonify(payload), 400

    folders_out = result["folders"]
    files_out = result["files"]
    norm = result["normalized_parent"]
    out = {
        "success": True,
        "parent": norm,
        "folders": folders_out,
        "files": files_out,
        "browse_method": result.get("method") or "",
    }
    if not folders_out and not files_out:
        hi = browse_empty_hint_i18n()
        out["hint_en"] = hi["en"]
        out["hint_ar"] = hi["ar"]
    return jsonify(out)


@sp_bp.route("/sp/explore")
def sp_explore_page():
    return redirect(url_for("system_bp.index") + "?tab=sp-explore")


@sp_bp.route("/sp/resolve_url", methods=["POST"])
def sp_resolve_url():
    from sp_sync.db.store import KEY_GUEST_LINK, get_store
    from sp_sync.sharepoint.sync_engine import (
        _sp_sharing_url_has_stream_path,
        resolve_sharepoint_pasted_url,
    )

    fedauth, rtfa = load_sp_cookies()
    if not fedauth or not rtfa:
        return _json_error("Please save FedAuth and rtFa first.", 401, code="MISSING_COOKIES")

    data = _request_json()
    url = (data.get("url") or "").strip()
    if not url:
        return _json_error("URL is empty.", 400, code="EMPTY_URL")

    if url.lower().startswith("http"):
        detected = detect_sharepoint_site_root_from_url(url)
        if detected:
            get_store().set_sharepoint_site_url(detected)

    if not sp_site():
        return _json_error(
            "SharePoint site URL is not set yet. Paste a full https SharePoint link once "
            "(site root is detected automatically), or set it under Connection settings.",
            400,
            code="MISSING_SITE_URL",
        )

    result = resolve_sharepoint_pasted_url(
        fedauth, rtfa, sp_site(), url, log=lambda msg: add_log("sharepoint", msg)
    )
    payload = dict(result)
    payload["success"] = bool(payload.pop("ok", False))
    if not payload["success"]:
        payload.setdefault("error", "Could not resolve URL.")
        payload.setdefault("code", "SP_RESOLVE_FAILED")
    if payload["success"] and url.lower().startswith("http"):
        if _sp_sharing_url_has_stream_path(url):
            share_u = url.strip()
            payload.setdefault("sharing_url", share_u)
            get_store().set_text(KEY_GUEST_LINK, share_u)
            add_log("sharepoint", "Guest :f:/ link saved from resolve")
        bp = (payload.get("browse_parent") or "").strip()
        if bp:
            payload.setdefault("sharing_root", bp)
    return jsonify(payload)


@sp_bp.route("/sp/bulk_add", methods=["POST"])
def sp_bulk_add():
    data = _request_json()
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return _json_error("Non-empty items array is required.", 400, code="INVALID_BULK_ITEMS")

    updates = {}
    skipped = 0
    for it in items:
        if not isinstance(it, dict):
            skipped += 1
            continue
        n = _normalize_sp_bulk_item(it)
        if not n["rel_url"] or not n["local_path"]:
            skipped += 1
            continue
        norm_row, val = validate_sp_config_row(n)
        if not val["valid"]:
            skipped += 1
            continue
        updates[norm_row["rel_url"]] = norm_row

    if not updates:
        return _json_error(
            "No valid items (each needs valid rel_url and writable local_path).",
            400,
            code="NO_VALID_ITEMS",
        )

    configs = load_sp_configs()
    existing_norm = {_normalize_sp_rel_key(c.get("rel_url", "")) for c in configs}

    new_list = []
    applied_urls = set()
    for c in configs:
        ru = _normalize_sp_rel_key(c.get("rel_url", ""))
        if ru in updates:
            merged = dict(updates[ru])
            if "sync_enabled" not in merged and "sync_enabled" in c:
                merged["sync_enabled"] = c["sync_enabled"]
            new_list.append(merged)
            applied_urls.add(ru)
        else:
            new_list.append(c)

    for ru, cfg in updates.items():
        if ru not in applied_urls:
            new_list.append(cfg)

    saved = save_sp_configs(new_list)
    new_entries = sum(1 for ru in updates if ru not in existing_norm)
    updated_entries = len(updates) - new_entries
    return jsonify(
        {
            "success": True,
            "merged": len(updates),
            "new": new_entries,
            "updated": updated_entries,
            "skipped_invalid": skipped,
            "total_configs": len(saved),
            "configs": saved,
        }
    )


@sp_bp.route("/sp/add_folder", methods=["POST"])
def sp_add_folder():
    data = _request_json()
    name = (data.get("name") or "").strip()
    rel_url = (data.get("rel_url") or "").strip()
    local_path = (data.get("local_path") or "").strip()

    if not rel_url or not local_path:
        return _json_error("Relative URL and local path are required.", 400, code="INVALID_SP_CONFIG")

    entry_type = (data.get("entry_type") or "folder").strip().lower()
    if entry_type not in ("folder", "file"):
        entry_type = "folder"

    row = {
        "name": name or "SharePoint Folder",
        "rel_url": rel_url,
        "local_path": local_path,
        "entry_type": entry_type,
        "sync_enabled": data.get("sync_enabled", True) is not False,
    }
    if entry_type == "folder" and "local_flat" in data and data.get("local_flat") is not None:
        row["local_flat"] = data["local_flat"]
    if "video_only" in data:
        row["video_only"] = bool(data.get("video_only"))
    share = (data.get("sharing_link") or "").strip()
    if share.lower().startswith("http"):
        row["sharing_link"] = share

    normalized, val = validate_sp_config_row(row)
    if not val["valid"]:
        return _json_error("; ".join(val["errors"]), 400, code="INVALID_SP_CONFIG", errors=val["errors"])

    configs = load_sp_configs()
    configs.append(normalized)
    save_sp_configs(configs)
    add_log("sharepoint", f"📁 Folder added: {normalized['name']}")
    return jsonify({"success": True, "config": normalized, "warnings": val.get("warnings", [])})


@sp_bp.route("/sp/remove_folder", methods=["POST"])
def sp_remove_folder():
    data = _request_json()
    try:
        index = int(data.get("index", -1))
    except (TypeError, ValueError):
        return _json_error("Invalid index.", 400, code="INVALID_INDEX")

    configs = load_sp_configs()
    if 0 <= index < len(configs):
        removed = configs.pop(index)
        save_sp_configs(configs)
        add_log("sharepoint", f"🗑️ Folder removed: {removed.get('name', 'unknown')}")
        return jsonify({"success": True})
    return _json_error("Invalid index.", 400, code="INVALID_INDEX")


@sp_bp.route("/sp/update_folder", methods=["POST"])
def sp_update_folder():
    data = _request_json()
    try:
        index = int(data.get("index", -1))
    except (TypeError, ValueError):
        return _json_error("Invalid index.", 400, code="INVALID_INDEX")

    name = (data.get("name") or "").strip()
    rel_url = (data.get("rel_url") or "").strip()
    local_path = (data.get("local_path") or "").strip()
    if not rel_url or not local_path:
        return _json_error("Relative URL and local path are required.", 400, code="INVALID_SP_CONFIG")

    entry_type = (data.get("entry_type") or "folder").strip().lower()
    if entry_type not in ("folder", "file"):
        entry_type = "folder"

    configs = load_sp_configs()
    if not (0 <= index < len(configs)):
        return _json_error("Invalid index.", 400, code="INVALID_INDEX")

    prev = configs[index]
    row = {
        "name": name or "SharePoint Folder",
        "rel_url": rel_url,
        "local_path": local_path,
        "entry_type": entry_type,
    }
    if entry_type == "folder":
        if "local_flat" in data and data.get("local_flat") is not None:
            row["local_flat"] = data["local_flat"]
        elif "local_flat" in prev:
            row["local_flat"] = prev["local_flat"]
    if "video_only" in data:
        row["video_only"] = bool(data.get("video_only"))
    elif "video_only" in prev:
        row["video_only"] = prev["video_only"]

    if "sharing_link" in data:
        share = (data.get("sharing_link") or "").strip()
        if share.lower().startswith("http"):
            row["sharing_link"] = share
    elif "sharing_link" in prev:
        row["sharing_link"] = prev["sharing_link"]

    if "sharing_root" in prev:
        row["sharing_root"] = prev["sharing_root"]
    if "sync_enabled" in data:
        row["sync_enabled"] = data.get("sync_enabled") is not False
    elif "sync_enabled" in prev:
        row["sync_enabled"] = prev["sync_enabled"]

    normalized, val = validate_sp_config_row(row)
    if not val["valid"]:
        return _json_error("; ".join(val["errors"]), 400, code="INVALID_SP_CONFIG", errors=val["errors"])

    configs[index] = normalized
    save_sp_configs(configs)
    add_log("sharepoint", f"📝 Folder updated: {normalized['name']}")
    return jsonify({"success": True, "config": normalized, "warnings": val.get("warnings", [])})


@sp_bp.route("/sp/toggle_sync", methods=["POST"])
def sp_toggle_sync():
    data = _request_json()
    try:
        index = int(data.get("index", -1))
    except (TypeError, ValueError):
        return _json_error("Invalid index.", 400, code="INVALID_INDEX")

    configs = load_sp_configs()
    if not (0 <= index < len(configs)):
        return _json_error("Invalid index.", 400, code="INVALID_INDEX")

    if "enabled" in data:
        enabled = data.get("enabled") is not False
    else:
        enabled = not _sp_sync_enabled(configs[index])

    configs[index]["sync_enabled"] = enabled
    save_sp_configs(configs)
    name = configs[index].get("name", "SharePoint")
    add_log("sharepoint", f"{'▶️ Sync enabled' if enabled else '⏸️ Sync paused'}: {name}")
    return jsonify({"success": True, "enabled": enabled, "index": index})


@sp_bp.route("/sp/reorder_folders", methods=["POST"])
def sp_reorder_folders():
    data = _request_json()
    configs = data.get("configs")
    if isinstance(configs, list) and configs:
        normalized = []
        for raw in configs:
            if not isinstance(raw, dict):
                continue
            n = _normalize_sp_bulk_item(raw)
            if n["rel_url"] and n["local_path"]:
                if "sharing_link" not in n and raw.get("sharing_link"):
                    n["sharing_link"] = (raw.get("sharing_link") or "").strip()
                normalized.append(n)
        if not normalized:
            return _json_error("No valid folder entries.", 400, code="INVALID_ORDER")
        save_sp_configs(normalized)
        add_log("sharepoint", f"📋 Sync order updated ({len(normalized)} item(s))")
        return jsonify({"success": True, "total": len(normalized)})

    order = data.get("order")
    current = load_sp_configs()
    if not isinstance(order, list) or len(order) != len(current):
        return _json_error("Invalid order array.", 400, code="INVALID_ORDER")
    try:
        indices = [int(i) for i in order]
    except (TypeError, ValueError):
        return _json_error("Order must be integer indices.", 400, code="INVALID_ORDER")
    if sorted(indices) != list(range(len(current))):
        return _json_error("Order must be a permutation of indices.", 400, code="INVALID_ORDER")
    reordered = [current[i] for i in indices]
    save_sp_configs(reordered)
    add_log("sharepoint", "📋 Sync order updated")
    return jsonify({"success": True, "total": len(reordered)})


@sp_bp.route("/sp/save_guest_link", methods=["POST"])
def sp_save_guest_link():
    """Save default guest link. Preserves per-folder sharing_link unless apply_to_all=true."""
    from sp_sync.db.store import KEY_GUEST_LINK, get_store

    data = _request_json()
    link = (data.get("guest_link") or data.get("url") or "").strip()
    apply_to_all = bool(data.get("apply_to_all", False))
    store = get_store()
    store.set_text(KEY_GUEST_LINK, link)
    updated_count = 0
    if link and apply_to_all:
        try:
            configs = store.get_sharepoint_configs()
            for c in configs:
                if c.get("sharing_link"):
                    c["sharing_link"] = link
                    updated_count += 1
            if updated_count:
                store.set_sharepoint_configs(configs)
        except Exception as e:
            add_log("sharepoint", f"⚠️ Failed to update config sharing links: {e}")
    add_log("sharepoint", "Guest share link saved" if link else "Guest share link cleared")
    return jsonify({"success": True, "updated_folders": updated_count})

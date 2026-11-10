"""
SharePoint REST: list child folders/files under a server-relative folder path.

Used by the web explorer (`/sp/browse`) with the same session pattern as URL resolve.
Handles path normalization (Unicode NFC, single unquote), verbose OData shapes,
optional $expand fallback, and __next pagination for large folders.
"""

from __future__ import annotations

import base64
import random
import time
import unicodedata
import urllib.parse
from typing import Any

# Pagination safety caps
_MAX_PAGES = 50
_MAX_ITEMS = 10000



# SPO $expand collections are often capped at the default list page size (100).
_EXPAND_PAGE_SIZE_HINT = 100


def _collection_next_url(block: Any) -> str | None:
    """Return a next-page URL from a verbose or v4 OData collection object, if present."""
    if not isinstance(block, dict):
        return None
    for key in ("__next", "@odata.nextLink", "odata.nextLink"):
        val = block.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def expand_collections_need_pagination(payload: dict[str, Any]) -> bool:
    """True when an $expand Folders/Files payload is truncated (next link or a full first page)."""
    if not isinstance(payload, dict):
        return False
    d = payload.get("d")
    if not isinstance(d, dict):
        return False
    for key in ("Folders", "Files"):
        block = d.get(key)
        if not isinstance(block, dict):
            continue
        if _collection_next_url(block):
            return True
        results = block.get("results")
        if isinstance(results, list) and len(results) >= _EXPAND_PAGE_SIZE_HINT:
            return True
    return False


_RETRY_HTTP_STATUS = {429, 500, 502, 503, 504}


def _request_get_with_retry(session, url: str, *, timeout: int, headers: dict[str, str] | None = None, retries: int = 2):
    """GET helper with small exponential backoff for transient HTTP/network errors."""
    delay = 0.4
    last_exc = None
    for attempt in range(retries + 1):
        try:
            req_kw: dict[str, Any] = {"timeout": timeout}
            if headers is not None:
                req_kw["headers"] = headers
            resp = session.get(url, **req_kw)
            if resp.status_code not in _RETRY_HTTP_STATUS or attempt >= retries:
                return resp
        except Exception as exc:
            last_exc = exc
            if attempt >= retries:
                raise

        # jitter to avoid request bursts under rate limiting
        sleep_for = delay + random.uniform(0, 0.25)
        time.sleep(sleep_for)
        delay = min(delay * 2, 2.5)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("GET retry logic exhausted unexpectedly")


def normalize_server_relative_folder_path(raw: str) -> str:
    """
    Normalize a SharePoint server-relative folder path for REST GetFolderByServerRelativeUrl.

    - Strip; drop query string; unify slashes; ensure leading '/'
    - Single urllib.parse.unquote pass (avoid double-encoding drift)
    - Unicode NFC (Arabic / composed characters alignment with server paths)
    """
    s = (raw or "").strip()
    if "?" in s:
        s = s.split("?", 1)[0].strip()
    s = s.replace("\\", "/").strip()
    if not s.startswith("/"):
        s = "/" + s.lstrip("/")
    s = urllib.parse.unquote(s)
    return unicodedata.normalize("NFC", s)


def encode_path_for_get_folder_param(server_relative_path: str) -> str:
    """Percent-encode path for use inside GetFolderByServerRelativeUrl('...'); keep '/' unencoded."""
    return urllib.parse.quote(server_relative_path, safe="/")


def _parse_collection_page(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """
    Extract one page of items and optional next URL from a SharePoint-style JSON body.

    Supports:
    - OData verbose collection: {"d": {"results": [...], "__next": "https://..."}}
    - OData v4-ish: {"value": [...], "@odata.nextLink": "..."} / {"odata.nextLink": "..."}
    """
    if not isinstance(payload, dict):
        return [], None

    if "value" in payload and isinstance(payload["value"], list):
        nxt = payload.get("@odata.nextLink") or payload.get("odata.nextLink")
        return list(payload["value"]), nxt if isinstance(nxt, str) and nxt.strip() else None

    d = payload.get("d")
    if isinstance(d, dict):
        if isinstance(d.get("results"), list):
            nxt = d.get("__next")
            return list(d["results"]), nxt if isinstance(nxt, str) and nxt.strip() else None

    return [], None


def extract_embedded_expand_results(payload: dict[str, Any], key: str) -> list[dict[str, Any]] | None:
    """
    If payload is a single-folder verbose expand, return child list for key 'Folders' or 'Files'.

    Shape: {"d": {"Folders": {"results": [...]}}, "Files": {"results": [...]}}}
    """
    if not isinstance(payload, dict):
        return None
    d = payload.get("d")
    if not isinstance(d, dict):
        return None
    block = d.get(key)
    if not isinstance(block, dict):
        return None
    r = block.get("results")
    if isinstance(r, list):
        return list(r)
    return None


class ODataFetchResult(tuple):
    """3-tuple (items, status_code, error) with content_type attribute for backward compatibility."""

    def __new__(cls, items: list[dict[str, Any]] | None, status_code: int, error: str, content_type: str | None = None):
        return super().__new__(cls, (items, status_code, error))

    def __init__(self, items: list[dict[str, Any]] | None, status_code: int, error: str, content_type: str | None = None):
        self.content_type = content_type


def fetch_odata_collection_all_pages(
    session,
    start_url: str,
    headers: dict[str, str] | None,
    *,
    timeout: int = 60,
) -> ODataFetchResult:
    """
    GET a collection URL and follow SharePoint verbose __next until exhausted or caps hit.

    Returns ODataFetchResult(items_or_None, last_http_status, error_text) with .content_type.
    """
    items: list[dict[str, Any]] = []
    url: str | None = start_url
    pages = 0
    last_status = 0
    content_type: str | None = None

    while url and pages < _MAX_PAGES and len(items) < _MAX_ITEMS:
        pages += 1
        r = _request_get_with_retry(
            session,
            url,
            timeout=timeout,
            headers=headers,
            retries=2,
        )
        last_status = r.status_code
        content_type = (r.headers.get("Content-Type") or "").split(";")[0].strip() or None
        if r.status_code != 200:
            return ODataFetchResult(None, r.status_code, (r.text or "")[:800], content_type)

        try:
            payload = r.json()
        except ValueError:
            return ODataFetchResult(None, r.status_code, "Invalid JSON in collection response", content_type)

        page_items, nxt = _parse_collection_page(payload)
        items.extend(page_items)
        if not nxt:
            break
        url = nxt.strip()

    if len(items) >= _MAX_ITEMS:
        return ODataFetchResult(items, last_status, "", content_type)

    return ODataFetchResult(items, last_status, "", content_type)


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", (s or "").strip())


def _folder_rows(folder_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for fo in folder_items:
        fn = _nfc(fo.get("Name") or "")
        if fn == "Forms" or fn.startswith("_"):
            continue
        rel = _nfc(fo.get("ServerRelativeUrl") or "")
        rel = rel.replace("\\", "/")
        if not rel:
            continue
        out.append({"name": fn, "rel_url": rel})
    return out


def _file_rows(file_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for fi in file_items:
        name = _nfc(fi.get("Name") or "")
        rel = _nfc(fi.get("ServerRelativeUrl") or "")
        rel = rel.replace("\\", "/")
        if not name or not rel:
            continue
        size = fi.get("Length", 0)
        out.append({"name": name, "rel_url": rel, "size": size})
    return out


def _fetch_split_children_by_urls(
    session,
    folders_url: str,
    files_url: str,
    headers: dict[str, str] | None,
    *,
    timeout: int,
) -> dict[str, Any]:
    """
    GET explicit Folders and Files collection URLs (with pagination). Returns ok + raw lists or error fields.
    """
    res_f = fetch_odata_collection_all_pages(
        session, folders_url, headers, timeout=timeout
    )
    folders_raw, st_f, err_f = res_f
    if folders_raw is None:
        snippet = err_f.replace("\n", " ")[:500]
        ct = getattr(res_f, "content_type", None)
        return {
            "ok": False,
            "folders_raw": [],
            "files_raw": [],
            "error": f"Folders HTTP {st_f}",
            "body_snippet": snippet,
            "content_type": ct,
            "upstream_http_status": st_f,
        }

    res_fl = fetch_odata_collection_all_pages(
        session, files_url, headers, timeout=timeout
    )
    files_raw, st_fl, err_fl = res_fl
    if files_raw is None:
        snippet = err_fl.replace("\n", " ")[:500]
        ct = getattr(res_fl, "content_type", None)
        return {
            "ok": False,
            "folders_raw": folders_raw,
            "files_raw": [],
            "error": f"Files HTTP {st_fl}",
            "body_snippet": snippet,
            "content_type": ct,
            "upstream_http_status": st_fl,
        }

    return {
        "ok": True,
        "folders_raw": folders_raw,
        "files_raw": files_raw,
        "error": "",
        "body_snippet": "",
        "content_type": None,
    }


def _fetch_split_children(
    session,
    api_root: str,
    headers: dict[str, str] | None,
    *,
    timeout: int,
) -> dict[str, Any]:
    """GET .../Folders and .../Files under a classic GetFolderByServerRelativeUrl('...') root."""
    return _fetch_split_children_by_urls(
        session,
        api_root + "/Folders",
        api_root + "/Files",
        headers,
        timeout=timeout,
    )


def _odata_atp_query_value(norm: str) -> str:
    """Encode OData @p literal for GetFolderByServerRelativeUrl(@p) (single-quoted path)."""
    lit = "'" + (norm or "").replace("'", "''") + "'"
    return urllib.parse.quote(lit, safe="")


def _fetch_split_children_odata_atp(
    session,
    base: str,
    norm: str,
    headers: dict[str, str] | None,
    *,
    timeout: int,
) -> dict[str, Any]:
    """
    Same as split, but path is passed as @p= query (avoids huge quoted segments in the path;
    some tenants allow this when the classic URL returns 401/403).
    """
    root = (base or "").strip().rstrip("/")
    qv = _odata_atp_query_value(norm)
    folders_u = f"{root}/_api/web/GetFolderByServerRelativeUrl(@p)/Folders?@p={qv}"
    files_u = f"{root}/_api/web/GetFolderByServerRelativeUrl(@p)/Files?@p={qv}"
    return _fetch_split_children_by_urls(session, folders_u, files_u, headers, timeout=timeout)


def _should_try_v2_shares(sharing_link_url: str | None) -> bool:
    """v2.0/shares is meaningful for short sharing links; long onedrive.aspx URLs use REST path listing."""
    s = (sharing_link_url or "").lower()
    return ":f:/" in s or ":w:/" in s or ":x:/" in s


def _v2_share_token_from_url(sharing_url: str) -> str:
    raw = sharing_url.strip().encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "u!" + b64


def _v2_shares_children_api_url(
    site_host_root: str,
    share_token: str,
    parent_norm: str,
    *,
    parent_item_id: str | None = None,
    share_root_norm: str | None = None,
) -> str:
    """
    Build v2.0/shares children URL for share root, a subfolder (by item id), or by path under share root.
    """
    base = (site_host_root or "").strip().rstrip("/")
    if parent_item_id:
        item = urllib.parse.quote(str(parent_item_id).strip(), safe="")
        return f"{base}/_api/v2.0/shares/{share_token}/driveItem/{item}/children"

    parent = normalize_server_relative_folder_path(parent_norm)
    root = (
        normalize_server_relative_folder_path(share_root_norm)
        if share_root_norm
        else ""
    )
    if root and parent != root:
        root_prefix = root.rstrip("/") + "/"
        if parent.startswith(root_prefix) or parent == root.rstrip("/"):
            rel = parent[len(root.rstrip("/")) :].lstrip("/")
            if rel:
                rel_enc = "/".join(
                    urllib.parse.quote(seg, safe="") for seg in rel.split("/") if seg
                )
                return (
                    f"{base}/_api/v2.0/shares/{share_token}/driveItem:/{rel_enc}:/children"
                )
    return f"{base}/_api/v2.0/shares/{share_token}/driveItem/children"


def _v2_shares_list_children(
    session,
    site_base_url: str,
    sharing_url: str,
    parent_norm: str,
    *,
    timeout: int,
    parent_item_id: str | None = None,
    share_root_norm: str | None = None,
) -> dict[str, Any] | None:
    """
    OneDrive/SPO v2.0 shares API: list children of the drive item backing a sharing link.

    rel_url for rows is parent_norm + '/' + name when ServerRelativeUrl is absent.
    Returns None only on unexpected errors; HTTP failures return a dict with ok=False.
    """
    try:
        p = urllib.parse.urlparse((site_base_url or "").strip())
        netloc = (p.netloc or "").lower()
        if not netloc or "sharepoint.com" not in netloc:
            return None
        root = f"{p.scheme}://{netloc}"
        sid = _v2_share_token_from_url(sharing_url)
        url = _v2_shares_children_api_url(
            root,
            sid,
            parent_norm,
            parent_item_id=parent_item_id,
            share_root_norm=share_root_norm,
        )
        prev_accept = session.headers.get("Accept")
        session.headers["Accept"] = "application/json"
        try:
            val: list[dict[str, Any]] = []
            cur_url: str | None = url
            pages = 0
            last_ct = None

            while cur_url and pages < _MAX_PAGES and len(val) < _MAX_ITEMS:
                pages += 1
                r = _request_get_with_retry(session, cur_url, timeout=timeout, retries=2)
                st = r.status_code
                last_ct = (r.headers.get("Content-Type") or "").split(";")[0].strip() or None
                if st != 200:
                    snippet = (r.text or "")[:500].replace("\n", " ")
                    return {
                        "ok": False,
                        "normalized_parent": normalize_server_relative_folder_path(parent_norm),
                        "folders": [],
                        "files": [],
                        "error": f"Shares API HTTP {st}",
                        "body_snippet": snippet,
                        "content_type": last_ct,
                        "method": "v2_shares",
                        "upstream_http_status": st,
                    }
                try:
                    j = r.json()
                except ValueError:
                    snippet = (r.text or "")[:500].replace("\n", " ")
                    return {
                        "ok": False,
                        "normalized_parent": normalize_server_relative_folder_path(parent_norm),
                        "folders": [],
                        "files": [],
                        "error": "Shares API returned non-JSON body",
                        "body_snippet": snippet,
                        "content_type": last_ct,
                        "method": "v2_shares",
                        "upstream_http_status": st,
                    }
                batch = j.get("value")
                if isinstance(batch, list):
                    val.extend(batch)
                nxt = j.get("@odata.nextLink") or j.get("odata.nextLink")
                cur_url = nxt.strip() if isinstance(nxt, str) and nxt.strip() else None

            parent = normalize_server_relative_folder_path(parent_norm)
            folders_out: list[dict[str, Any]] = []
            files_out: list[dict[str, Any]] = []
            for item in val:
                if not isinstance(item, dict):
                    continue
                name = _nfc(item.get("name") or "")
                if not name or name == "Forms" or name.startswith("_"):
                    continue
                is_folder = isinstance(item.get("folder"), dict)
                rel = parent.rstrip("/") + "/" + name
                if is_folder:
                    row = {"name": name, "rel_url": rel.replace("\\", "/")}
                    item_id = item.get("id")
                    if isinstance(item_id, str) and item_id.strip():
                        row["item_id"] = item_id.strip()
                    folders_out.append(row)
                else:
                    sz = item.get("size", 0) or 0
                    row = {"name": name, "rel_url": rel.replace("\\", "/"), "size": sz}
                    item_id = item.get("id")
                    if isinstance(item_id, str) and item_id.strip():
                        row["item_id"] = item_id.strip()
                    files_out.append(row)
            return {
                "ok": True,
                "normalized_parent": parent,
                "folders": folders_out,
                "files": files_out,
                "error": "",
                "body_snippet": "",
                "content_type": last_ct,
                "method": "v2_shares",
            }
        finally:
            if prev_accept:
                session.headers["Accept"] = prev_accept
            else:
                session.headers["Accept"] = "application/json;odata=verbose"
    except Exception:
        return None


def _web_relative_folder_path(site_base_url: str, full_server_relative: str) -> str | None:
    """
    If site_base_url is https://host/personal/user, some REST calls need /Documents/... only.

    Returns None if no usable shorter path.
    """
    try:
        p = urllib.parse.urlparse(site_base_url)
        web_path = (p.path or "").rstrip("/")
    except Exception:
        return None
    if not web_path or web_path == "/":
        return None
    full = full_server_relative
    if not full.startswith(web_path + "/") and full != web_path:
        return None
    suffix = full[len(web_path) :]
    if not suffix.startswith("/") or len(suffix) < 2:
        return None
    return suffix


def list_folder_children_rest(
    session,
    site_base_url: str,
    server_relative_folder_path: str,
    *,
    timeout: int = 60,
    sharing_link_url: str | None = None,
    parent_item_id: str | None = None,
    share_root_norm: str | None = None,
) -> dict[str, Any]:
    """
    List immediate child folders and files under server_relative_folder_path.

    Returns a dict:
      ok: bool
      normalized_parent: str
      folders: list[dict]
      files: list[dict]
      error: str (if not ok)
      body_snippet: str (HTTP error bodies, truncated)
      content_type: str | None (on HTTP error)
      method: str — 'expand' | 'split' | 'split_odata_atp' | 'split_webrel' | 'v2_shares'
    """
    base = (site_base_url or "").strip().rstrip("/")
    norm = normalize_server_relative_folder_path(server_relative_folder_path)
    enc = encode_path_for_get_folder_param(norm)
    api_root = f"{base}/_api/web/GetFolderByServerRelativeUrl('{enc}')"
    method_used = "split"

    share_u = (sharing_link_url or "").strip()
    if share_u.startswith("http") and _should_try_v2_shares(share_u):
        v2_first = _v2_shares_list_children(
            session,
            base,
            share_u,
            norm,
            timeout=timeout,
            parent_item_id=parent_item_id,
            share_root_norm=share_root_norm,
        )
        if v2_first and v2_first.get("ok"):
            return v2_first
        if v2_first and not v2_first.get("ok"):
            st_v2 = v2_first.get("upstream_http_status")
            if st_v2 in (401, 403, 404):
                return v2_first

    # 1) Try single GET with $expand=Folders,Files (one round-trip, consistent snapshot)
    expand_url = api_root + "?$expand=Folders,Files"
    r_exp = session.get(expand_url, timeout=timeout)
    if r_exp.status_code == 200:
        try:
            exp_json = r_exp.json()
        except ValueError:
            exp_json = {}
        f_exp = extract_embedded_expand_results(exp_json, "Folders")
        fi_exp = extract_embedded_expand_results(exp_json, "Files")
        if f_exp is not None and fi_exp is not None:
            expand_folders = _folder_rows(f_exp)
            expand_files = _file_rows(fi_exp)
            # OneDrive / SPO often returns inline $expand with empty results even when
            # /Folders and /Files list children. Only trust expand when it returns something.
            # $expand also does not follow __next, so a truncated first page must fall through
            # to the paginated /Folders and /Files split.
            if (expand_folders or expand_files) and not expand_collections_need_pagination(exp_json):
                return {
                    "ok": True,
                    "normalized_parent": norm,
                    "folders": expand_folders,
                    "files": expand_files,
                    "error": "",
                    "body_snippet": "",
                    "content_type": (r_exp.headers.get("Content-Type") or "").split(";")[0].strip() or None,
                    "method": "expand",
                }

    # 2) Fallback: separate /Folders and /Files with pagination
    split = _fetch_split_children(session, api_root, None, timeout=timeout)
    st = split.get("upstream_http_status")
    if not split["ok"] and st in (401, 403):
        split_od = _fetch_split_children_odata_atp(session, base, norm, None, timeout=timeout)
        if split_od["ok"]:
            split = split_od
            method_used = "split_odata_atp"

    if not split["ok"]:
        su = (sharing_link_url or "").strip()
        if su.startswith("http") and _should_try_v2_shares(su):
            v2 = _v2_shares_list_children(
                session,
                base,
                su,
                norm,
                timeout=timeout,
                parent_item_id=parent_item_id,
                share_root_norm=share_root_norm,
            )
            if v2 and v2.get("ok"):
                return v2
        return {
            "ok": False,
            "normalized_parent": norm,
            "folders": _folder_rows(split.get("folders_raw") or []),
            "files": _file_rows(split.get("files_raw") or []),
            "error": split.get("error") or "Browse failed",
            "body_snippet": split.get("body_snippet") or "",
            "content_type": split.get("content_type"),
            "method": method_used,
            "upstream_http_status": split.get("upstream_http_status"),
        }

    folders_raw = split["folders_raw"]
    files_raw = split["files_raw"]
    folder_rows = _folder_rows(folders_raw)
    file_rows = _file_rows(files_raw)

    # 3) Some OneDrive webs list children only when the path is web-root-relative (/Documents/...)
    if not folder_rows and not file_rows:
        rel_web = _web_relative_folder_path(base, norm)
        if rel_web:
            enc_w = encode_path_for_get_folder_param(rel_web)
            api_web = f"{base}/_api/web/GetFolderByServerRelativeUrl('{enc_w}')"
            split_w = _fetch_split_children(session, api_web, None, timeout=timeout)
            if not split_w["ok"] and split_w.get("upstream_http_status") in (401, 403):
                split_w_od = _fetch_split_children_odata_atp(session, base, rel_web, None, timeout=timeout)
                if split_w_od["ok"]:
                    split_w = split_w_od
            if split_w["ok"]:
                folder_rows = _folder_rows(split_w["folders_raw"])
                file_rows = _file_rows(split_w["files_raw"])
                if folder_rows or file_rows:
                    return {
                        "ok": True,
                        "normalized_parent": norm,
                        "folders": folder_rows,
                        "files": file_rows,
                        "error": "",
                        "body_snippet": "",
                        "content_type": None,
                        "method": "split_webrel",
                    }

    if not folder_rows and not file_rows:
        su = (sharing_link_url or "").strip()
        if su.startswith("http") and _should_try_v2_shares(su):
            v2 = _v2_shares_list_children(
                session,
                base,
                su,
                norm,
                timeout=timeout,
                parent_item_id=parent_item_id,
                share_root_norm=share_root_norm,
            )
            if v2 and v2.get("ok"):
                return v2

    return {
        "ok": True,
        "normalized_parent": norm,
        "folders": folder_rows,
        "files": file_rows,
        "error": "",
        "body_snippet": "",
        "content_type": None,
        "method": method_used,
    }


def browse_empty_hint_i18n() -> dict[str, str]:
    """Separate EN/AR strings for explorer empty-folder hint (no mixed copy)."""
    return {
        "en": (
            "No files or subfolders were returned for this path. If you still see items in the browser, "
            "use Resolve link again with a fresh URL or update FedAuth/rtFa in Connection settings."
        ),
        "ar": (
            "لم يُرجع الخادم أي ملفات أو مجلدات فرعية لهذا المسار. إن كان المحتوى يظهر في المتصفح، "
            "جرّب إعادة Resolve برابط جديد أو حدّث FedAuth/rtFa من إعدادات الاتصال."
        ),
    }


def browse_empty_hint() -> str:
    """Backward-compatible single string (English only) for callers/tests."""
    return browse_empty_hint_i18n()["en"]

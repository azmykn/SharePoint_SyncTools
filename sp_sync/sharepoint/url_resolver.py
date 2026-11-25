"""SharePoint / OneDrive URL parsing, HTML path extraction, and sharing-link resolution.

Extracted from ``sp_sync.sharepoint.sync_engine`` for modularity; all symbols are
re-exported from ``sync_engine`` to maintain full backward compatibility.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from pathlib import PurePosixPath
from typing import Optional, Tuple

import requests

_logger = logging.getLogger(__name__)


def _sp_url_leaf_segment(server_rel_url: str) -> str:
    """Last path segment of a SharePoint server-relative URL (decoded)."""
    parts = [p for p in (server_rel_url or "").replace("\\", "/").split("/") if p]
    if not parts:
        return "SharePoint"
    return urllib.parse.unquote(parts[-1])


def _sanitize_dir_name(name: str) -> str:
    """Make a single path component safe on Windows and POSIX filesystems."""
    if not name or not str(name).strip():
        return "SharePoint"
    bad = '<>:"/\\|?*'
    s = "".join("_" if (c in bad or ord(c) < 32) else c for c in str(name).strip())
    s = s.strip(" .")
    if not s or s in (".", ".."):
        return "SharePoint"
    if len(s) > 200:
        s = s[:200].rstrip(" .") or "SharePoint"
    return s


def _sp_resolve_normalize_input(pasted_url: str):
    u = (pasted_url or "").strip()
    if not u:
        return None, ""
    if u.startswith("/"):
        return "server_path", urllib.parse.unquote(u)
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return "url", u


def _sp_split_path_and_query(s: str) -> Tuple[str, str]:
    s = (s or "").strip()
    if "?" in s:
        path_part, q = s.split("?", 1)
        return path_part.strip(), q.strip()
    return s, ""


def _sp_looks_like_onedrive_sharing_stub(path_no_query: str) -> bool:
    """/personal/{upn}/{token} where token is a :f:/g sharing resource id (not Documents/...)."""
    p = path_no_query.replace("\\", "/").strip()
    if not p.startswith("/personal/"):
        return False
    parts = [x for x in p.split("/") if x]
    if len(parts) != 3 or parts[0] != "personal":
        return False
    tail = parts[2]
    if tail in (
        "Documents",
        "Shared Documents",
        "Microsoft Teams Chat Files",
        "Attachments",
    ):
        return False
    if len(tail) < 20:
        return False
    if not re.match(r"^[A-Za-z0-9_-]+$", tail):
        return False
    return True


def _sp_unquote_path_value(raw: str) -> str:
    """Decode id/folder query values; some tenants double-encode %2F."""
    s = (raw or "").strip().replace("+", " ")
    if not s:
        return ""
    s = urllib.parse.unquote(s)
    if "%" in s:
        s2 = urllib.parse.unquote(s)
        if s2 != s:
            s = s2
    return s


def _sp_extract_onedrive_id_param(url: str) -> Optional[str]:
    """Extract server-relative path from id= / folder= / RootFolder= / res= query (decoded)."""
    if not (isinstance(url, str) and url.strip()):
        return None
    parsed = urllib.parse.urlparse(url.strip())
    chunks = []
    if parsed.query:
        chunks.append(parsed.query)
    frag = (parsed.fragment or "").lstrip("?")
    frag_unq = urllib.parse.unquote(frag) if frag else ""
    if frag_unq and ("=" in frag_unq or "%3d" in frag.lower()):
        chunks.append(frag_unq)
    combined = "&".join([c for c in chunks if c])
    if not combined:
        return None
    qs = urllib.parse.parse_qs(combined, keep_blank_values=False)
    for k, vals in qs.items():
        lk = (k or "").lower()
        if lk not in ("id", "folder", "res", "rootfolder"):
            continue
        if not vals or not str(vals[0]).strip():
            continue
        decoded = _sp_unquote_path_value(str(vals[0]))
        if decoded:
            return decoded
    if frag_unq:
        for pat in (
            r"(?:^|[?&])id=([^&]+)",
            r"(?:^|[?&])folder=([^&]+)",
            r"(?:^|[?&])rootfolder=([^&]+)",
        ):
            m = re.search(pat, frag_unq, re.I)
            if m:
                decoded = _sp_unquote_path_value(m.group(1))
                if decoded:
                    return decoded
    return None


def _sp_extract_server_rel_from_sharepoint_html(html: str) -> Optional[str]:
    """Best-effort path extraction from OneDrive/SPO HTML after redirects (SPA / mixed markup)."""
    if not html or len(html) < 20:
        return None

    def _norm_path(s: str) -> Optional[str]:
        s = (s or "").strip().replace("&amp;", "&").replace("\\/", "/")
        if not s:
            return None
        s = _sp_unquote_path_value(s)
        if s.startswith("http"):
            try:
                p = urllib.parse.urlparse(s)
                s = _sp_unquote_path_value(p.path or "")
            except Exception:
                return None
        if not s.startswith("/"):
            s = "/" + s.lstrip("/")
        low = s.lower()
        if "/personal/" not in low and "/sites/" not in low:
            return None
        return s

    for pat in (
        r'<meta\s+property=["\']og:url["\']\s+content=["\']([^"\']+)["\']',
        r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:url["\']',
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
        r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']',
    ):
        m = re.search(pat, html, re.I)
        if not m:
            continue
        href = m.group(1).strip()
        cand = _sp_extract_onedrive_id_param(href) or _norm_path(href)
        if cand:
            return cand

    for pat in (
        r'"serverRelativeUrl"\s*:\s*"([^"]+)"',
        r"'serverRelativeUrl'\s*:\s*'([^']+)'",
        r'"listUrl"\s*:\s*"([^"]+)"',
        r"'listUrl'\s*:\s*'([^']+)'",
    ):
        m = re.search(pat, html, re.I)
        if not m:
            continue
        cand = _norm_path(m.group(1))
        if cand:
            return cand

    for pat in (
        r'(?:onedrive|embed)\.aspx[^"\'<>\s]*[?&]id=([^&"\'<>\s]+)',
        r'(?:onedrive|embed)\.aspx[^"\'<>\s]*[?&]folder=([^&"\'<>\s]+)',
        r'[?&]RootFolder=([^&"\'<>\s]+)',
    ):
        m = re.search(pat, html, re.I)
        if not m:
            continue
        cand = _norm_path(m.group(1))
        if cand:
            return cand

    return None


def _sp_sharing_url_has_stream_path(url: str) -> bool:
    """OneDrive-style sharing URL path contains :f:/, :w:/, or :x:/ (not plain /personal/...)."""
    low = (url or "").lower()
    return ":f:/" in low or ":w:/" in low or ":x:/" in low


def _sp_body_indicates_sharing_link_removed(html: str) -> bool:
    """Detect SharePoint 'link removed / access removed' HTML (EN and common AR phrases)."""
    if not html:
        return False
    low = html.lower()
    if "this link has been removed" in low:
        return True
    if "access to this document has been removed" in low:
        return True
    if "the link has been removed" in low:
        return True
    if "link has been removed" in low and "sharepoint" in low:
        return True
    if "تمت إزالة هذا الرابط" in html or "تم إزالة هذا الرابط" in html:
        return True
    if "لم يعد بإمكانك الوصول" in html and "رابط" in html:
        return True
    return False


def _sp_server_rel_from_personal_browser_path(parsed_url) -> Optional[str]:
    """If URL path is /personal/... without :f:/ sharing token in path, return server-relative path."""
    path = parsed_url.path or ""
    if "/personal/" not in path:
        return None
    pl = path.lower()
    if ":f:/" in pl or ":w:/" in pl or ":x:/" in pl:
        return None
    if "onedrive.aspx" in pl:
        return None
    if not path.startswith("/"):
        path = "/" + path.lstrip("/")
    return urllib.parse.unquote(path) or None


def _sp_resolve_via_v2_share_driveitem(session, sharing_url: str) -> Tuple[Optional[str], Optional[int]]:
    """Resolve :f:/… / :w:/… sharing URLs via _api/v2.0/shares/u!…/driveItem (no HTML redirect)."""
    from sp_sync.sharepoint.rest_folder_list import (
        _request_get_with_retry,
        _v2_share_token_from_url,
    )

    u = (sharing_url or "").strip()
    if not u.startswith("http") or not _sp_sharing_url_has_stream_path(u):
        return None, None
    try:
        p = urllib.parse.urlparse(u)
        host = (p.netloc or "").lower()
        if "sharepoint.com" not in host:
            return None, None
        sid = _v2_share_token_from_url(u)
        api = f"{p.scheme}://{p.netloc}/_api/v2.0/shares/{sid}/driveItem"
        r = _request_get_with_retry(
            session, api, timeout=60, headers={"Accept": "application/json"}, retries=2
        )
        st = r.status_code
        if st != 200:
            return None, st
        j = r.json()
    except Exception as exc:
        _logger.warning("v2 share driveItem resolve failed: %s", exc)
        return None, None

    if not isinstance(j, dict):
        return None, None

    for key in ("webUrl", "webDavUrl"):
        val = j.get(key)
        if not isinstance(val, str) or not val.strip():
            continue
        ex = _sp_extract_onedrive_id_param(val)
        if ex:
            return ex, 200
        pp = urllib.parse.urlparse(val.strip())
        sp = _sp_server_rel_from_personal_browser_path(pp)
        if sp:
            return sp, 200

    spids = j.get("sharepointIds")
    if isinstance(spids, dict):
        for sk in ("listPath", "sitePath"):
            sp_path = spids.get(sk)
            if isinstance(sp_path, str) and "/personal/" in sp_path.lower():
                s = sp_path.strip()
                if not s.startswith("/"):
                    s = "/" + s.lstrip("/")
                return urllib.parse.unquote(s), 200

    return None, 200


def _sp_folder_rest_ok(session, sp_base: str, server_rel: str) -> bool:
    from sp_sync.sharepoint.rest_folder_list import (
        _request_get_with_retry,
        encode_path_for_get_folder_param,
        normalize_server_relative_folder_path,
    )

    norm = normalize_server_relative_folder_path(server_rel)
    enc = encode_path_for_get_folder_param(norm)
    api = f"{sp_base.rstrip('/')}/_api/web/GetFolderByServerRelativeUrl('{enc}')"
    try:
        r = _request_get_with_retry(session, api, timeout=45, retries=1)
        return r.status_code == 200
    except Exception:
        return False


def _sp_ensure_browse_folder(session, sp_base: str, server_rel: str, log) -> Optional[str]:
    """Walk up parents until GetFolder succeeds (link may target a file)."""
    cur = (server_rel or "").replace("\\", "/").strip()
    if not cur.startswith("/"):
        cur = "/" + cur
    cur = urllib.parse.unquote(cur)
    seen = set()
    while cur and cur not in seen and len(seen) < 40:
        seen.add(cur)
        if _sp_folder_rest_ok(session, sp_base, cur):
            if len(seen) > 1:
                log(f"  ✓ Using browse folder: {cur}")
            return cur
        parent = str(PurePosixPath(cur).parent)
        if parent == cur:
            break
        log("  ↪ Path is not a listable folder; trying parent...")
        cur = parent
    return None

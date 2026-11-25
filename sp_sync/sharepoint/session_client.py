"""SharePoint REST HTTP session creation, warm-up priming, FormDigest, and streaming retries."""

from __future__ import annotations

import logging
import random
import re
import threading
import time
import urllib.parse
from typing import Optional

import requests

_logger = logging.getLogger(__name__)

_TRANSIENT_DOWNLOAD_STATUS = {429, 500, 502, 503, 504}


def _sharepoint_cookie_host(sp_site_url: str, fallback_site_url: str = "") -> str:
    """Hostname for FedAuth/rtFa cookie domain (e.g. tenant-my.sharepoint.com)."""
    return urllib.parse.urlparse(sp_site_url or fallback_site_url).netloc


def _sp_share_requests_session(
    sp_site_url: str,
    fedauth_cookie: str,
    rtfa_cookie: str,
    accept_html: bool = False,
    *,
    fallback_site_url: str = "",
) -> requests.Session:
    cookie_host = _sharepoint_cookie_host(sp_site_url, fallback_site_url)
    session = requests.Session()
    accept = (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        if accept_html
        else "application/json;odata=verbose"
    )
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
        }
    )
    if fedauth_cookie:
        session.cookies.set("FedAuth", fedauth_cookie, domain=cookie_host, path="/")
    if rtfa_cookie:
        session.cookies.set("rtFa", rtfa_cookie, domain=cookie_host, path="/")
    return session


def extract_request_digest_from_html(html: str) -> Optional[str]:
    """Parse SharePoint / OneDrive __REQUESTDIGEST from HTML (matches browser X-RequestDigest)."""
    if not html:
        return None
    m = re.search(
        r'name=["\']__REQUESTDIGEST["\']\s+value=["\']([^"\']+)["\']',
        html,
        re.I,
    )
    if m:
        return m.group(1).strip()
    m = re.search(
        r'id=["\']__REQUESTDIGEST["\'][^>]*value=["\']([^"\']+)["\']',
        html,
        re.I,
    )
    if m:
        return m.group(1).strip()
    return None


def apply_sharepoint_page_digest(session: requests.Session, page_url: str, timeout: int = 45) -> None:
    """GET a SharePoint HTML page and copy __REQUESTDIGEST into session headers when present."""
    u = (page_url or "").strip()
    if not u.startswith("http"):
        return
    try:
        r = session.get(
            u,
            timeout=timeout,
            allow_redirects=True,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        d = extract_request_digest_from_html(r.text or "")
        if d:
            session.headers["X-RequestDigest"] = d
    except Exception as exc:
        _logger.debug("Page digest fetch skipped for %s: %s", u, exc)


def attach_sharepoint_browser_rest_hints(
    session: requests.Session, sp_site_url: str, referer_url: Optional[str] = None
) -> None:
    """Set headers SharePoint Online endpoints expect for XHR-style REST calls."""
    ref = (referer_url or "").strip()
    if not ref.startswith("http"):
        ref = (sp_site_url or "").strip().rstrip("/") + "/"
    session.headers["Referer"] = ref
    session.headers["X-Requested-With"] = "XMLHttpRequest"


def try_attach_sharepoint_form_digest(
    session: requests.Session, sp_site_url: str, timeout: int = 30
) -> None:
    """POST /_api/contextinfo and set X-RequestDigest on session when SharePoint allows it."""
    base = (sp_site_url or "").strip().rstrip("/")
    if not base:
        return
    url = base + "/_api/contextinfo"
    try:
        r = session.post(
            url,
            data="{}",
            headers={
                "Accept": "application/json;odata=verbose",
                "Content-Type": "application/json;odata=verbose",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=timeout,
        )
        if r.status_code != 200:
            return
        j = r.json()
        d = j.get("d") or {}
        gci = d.get("GetContextWebInformation")
        if isinstance(gci, dict):
            digest = gci.get("FormDigestValue")
            if digest:
                session.headers["X-RequestDigest"] = digest
    except Exception as exc:
        _logger.debug("Contextinfo digest unavailable: %s", exc)


def apply_sharepoint_chromium_security_headers(session: requests.Session, sp_site_url: str) -> None:
    """Optional Sec-Fetch-* / Origin headers some SPO WAFs correlate with real browsers."""
    try:
        p = urllib.parse.urlparse((sp_site_url or "").strip())
        origin = f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""
        if origin:
            session.headers["Origin"] = origin
        session.headers.setdefault("Sec-Fetch-Dest", "empty")
        session.headers.setdefault("Sec-Fetch-Mode", "cors")
        session.headers.setdefault("Sec-Fetch-Site", "same-origin")
    except Exception:
        pass


def stream_get_with_retry(
    session: requests.Session,
    url: str,
    *,
    headers: Optional[dict] = None,
    timeout: int = 60,
    allow_redirects: bool = True,
    retries: int = 2,
    base_delay: float = 0.8,
    cancel_event: Optional[threading.Event] = None,
    log=None,
) -> requests.Response:
    """Streaming HTTP GET with bounded retry on transient 429/5xx and network errors."""
    delay = base_delay
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        if cancel_event and cancel_event.is_set():
            raise InterruptedError("SharePoint sync cancelled by user")
        try:
            req_kw = {"stream": True, "timeout": timeout, "allow_redirects": allow_redirects}
            if headers is not None:
                req_kw["headers"] = headers
            resp = session.get(url, **req_kw)
            if resp.status_code not in _TRANSIENT_DOWNLOAD_STATUS or attempt >= retries:
                return resp
            if log:
                log(f"    ⚠️ Transient HTTP {resp.status_code} on download; retrying ({attempt + 1}/{retries})...")
            try:
                resp.close()
            except Exception:
                pass
        except (requests.RequestException, TimeoutError, ConnectionError, OSError) as exc:
            last_exc = exc
            if attempt >= retries:
                raise
            if log:
                log(f"    ⚠️ Network error on download ({exc}); retrying ({attempt + 1}/{retries})...")
        sleep_for = delay + random.uniform(0.0, 0.35)
        if cancel_event:
            if cancel_event.wait(sleep_for):
                raise InterruptedError("SharePoint sync cancelled by user")
        else:
            time.sleep(sleep_for)
        delay = min(delay * 2.0, 8.0)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Download retry loop exhausted unexpectedly")

"""Optional Telegram Bot notifications (bot token & chat ID stored Fernet-encrypted in SQLite)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

import requests

_logger = logging.getLogger(__name__)

DEFAULT_TELEGRAM_SETTINGS: Dict[str, Any] = {
    "enabled": False,
    "bot_token": "",
    "chat_id": "",
    "notify_on_success": True,
    "notify_on_error": True,
    "notify_on_cookie_expiry": True,
    "last_cookie_alert_ts": 0.0,
}

_COOKIE_ALERT_COOLDOWN_SEC = 6 * 3600.0


def _mask_token(tok: str) -> str:
    s = (tok or "").strip()
    if len(s) <= 8:
        return "****" if s else ""
    return f"{s[:4]}...{s[-4:]}"


def get_telegram_settings(*, masked: bool = True) -> Dict[str, Any]:
    """Return current Telegram notification settings (token masked by default for Web UI safety)."""
    from sp_sync.db.store import get_store

    merged = dict(DEFAULT_TELEGRAM_SETTINGS)
    raw = get_store().get_telegram_settings_raw()
    if isinstance(raw, dict):
        for k in DEFAULT_TELEGRAM_SETTINGS:
            if k in raw:
                merged[k] = raw[k]

    token = str(merged.get("bot_token") or "").strip()
    out = {
        "enabled": bool(merged.get("enabled", False)),
        "chat_id": str(merged.get("chat_id") or "").strip(),
        "notify_on_success": bool(merged.get("notify_on_success", True)),
        "notify_on_error": bool(merged.get("notify_on_error", True)),
        "notify_on_cookie_expiry": bool(merged.get("notify_on_cookie_expiry", True)),
        "has_bot_token": bool(token),
        "bot_token_hint": _mask_token(token),
    }
    if not masked:
        out["bot_token"] = token
        out["last_cookie_alert_ts"] = float(merged.get("last_cookie_alert_ts") or 0.0)
    return out


def save_telegram_settings(updates: dict) -> Dict[str, Any]:
    """Validate and save Telegram settings into encrypted SQLite storage."""
    from sp_sync.db.store import get_store

    if not isinstance(updates, dict):
        raise ValueError("Invalid Telegram settings payload.")

    current = get_telegram_settings(masked=False)
    enabled = bool(updates.get("enabled", current["enabled"]))
    chat_id = str(updates.get("chat_id") if "chat_id" in updates else current["chat_id"]).strip()

    # Keep existing token if input is omitted or blank (unless explicitly cleared)
    raw_token = updates.get("bot_token")
    if updates.get("clear_token"):
        bot_token = ""
    elif isinstance(raw_token, str) and raw_token.strip():
        bot_token = raw_token.strip()
    else:
        bot_token = current["bot_token"]

    if enabled:
        if not bot_token or ":" not in bot_token:
            raise ValueError("Valid Telegram Bot Token (format 123456:ABC...) is required when enabled.")
        if not chat_id:
            raise ValueError("Telegram Chat ID is required when enabled.")

    payload = {
        "enabled": enabled,
        "bot_token": bot_token,
        "chat_id": chat_id,
        "notify_on_success": bool(updates.get("notify_on_success", current["notify_on_success"])),
        "notify_on_error": bool(updates.get("notify_on_error", current["notify_on_error"])),
        "notify_on_cookie_expiry": bool(updates.get("notify_on_cookie_expiry", current["notify_on_cookie_expiry"])),
        "last_cookie_alert_ts": float(current.get("last_cookie_alert_ts") or 0.0),
    }
    get_store().set_telegram_settings_raw(payload)
    return get_telegram_settings(masked=True)


def send_telegram_message(
    text: str,
    *,
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    timeout: int = 12,
) -> Tuple[bool, str]:
    """Send a plain text notification via Telegram Bot API without leaking bot tokens."""
    cfg = get_telegram_settings(masked=False)
    tok = (bot_token if bot_token is not None else cfg["bot_token"]).strip()
    cid = (chat_id if chat_id is not None else cfg["chat_id"]).strip()
    if not tok or not cid:
        return False, "Telegram bot token or chat ID is not configured."

    url = f"https://api.telegram.org/bot{tok}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": cid, "text": str(text)[:3900], "disable_web_page_preview": True},
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("ok"):
                return True, "Telegram notification sent."
            return False, str(data.get("description") or "Telegram API rejected message.")
        try:
            desc = resp.json().get("description") or f"HTTP {resp.status_code}"
        except Exception:
            desc = f"HTTP {resp.status_code}"
        return False, f"Telegram API error: {desc}"
    except requests.RequestException as exc:
        # Sanitize token from any request exception string
        safe_err = str(exc).replace(tok, "***")
        _logger.warning("Telegram send failed: %s", safe_err)
        return False, f"Network error: {safe_err}"


def test_telegram_connection(bot_token: Optional[str] = None, chat_id: Optional[str] = None) -> Tuple[bool, str]:
    """Send a test ping message to verify Telegram bot token & chat ID."""
    return send_telegram_message(
        "✅ Unified Sync Tool: Telegram notifications are connected and working!",
        bot_token=bot_token,
        chat_id=chat_id,
    )


def notify_sync_summary(summary: dict) -> bool:
    """Send a concise Telegram summary after a scheduled sync completes (if enabled)."""
    cfg = get_telegram_settings(masked=False)
    if not cfg["enabled"] or not cfg["bot_token"] or not cfg["chat_id"]:
        return False

    errors = list(summary.get("errors") or [])
    new_files = list(summary.get("new_files") or [])
    downloaded = int(summary.get("downloaded_files") or 0)
    target = str(summary.get("target") or "all")
    finished = str(summary.get("finished_at") or "")

    if errors and not cfg["notify_on_error"]:
        return False
    if not errors and not cfg["notify_on_success"]:
        return False
    # Skip silent empty scheduled runs unless there were downloads or errors
    if downloaded == 0 and not errors:
        return False

    lines = [
        f"{'⚠️' if errors else '✅'} Unified Sync Summary ({target})",
        f"🕒 {finished}",
        f"📥 Downloaded: {downloaded} file(s)",
    ]
    if new_files:
        preview = "\n".join(f"• {fn}" for fn in new_files[:10])
        lines.append(preview)
        if len(new_files) > 10:
            lines.append(f"… and {len(new_files) - 10} more")
    if errors:
        lines.append("❌ Errors:")
        lines.extend(f"• {e}" for e in errors[:5])

    ok, _msg = send_telegram_message("\n".join(lines))
    return ok


def notify_cookie_expired(detail: str = "") -> bool:
    """Alert on Telegram when SharePoint cookies expire (rate-limited to once per 6 hours)."""
    from sp_sync.db.store import get_store

    cfg = get_telegram_settings(masked=False)
    if not cfg["enabled"] or not cfg["notify_on_cookie_expiry"] or not cfg["bot_token"] or not cfg["chat_id"]:
        return False

    now = time.time()
    if now - float(cfg.get("last_cookie_alert_ts") or 0.0) < _COOKIE_ALERT_COOLDOWN_SEC:
        return False

    msg = (
        "⚠️ Unified Sync Tool: SharePoint cookies have expired or need refresh.\n"
        + (f"Details: {detail}\n" if detail else "")
        + "Open Settings → Connections to paste fresh FedAuth & rtFa cookies."
    )
    ok, _err = send_telegram_message(msg)
    if ok:
        raw = get_store().get_telegram_settings_raw()
        raw["last_cookie_alert_ts"] = now
        get_store().set_telegram_settings_raw(raw)
    return ok

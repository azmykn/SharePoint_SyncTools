"""Optional external notification channels (Telegram Bot API)."""

from sp_sync.notifications.telegram import (
    get_telegram_settings,
    notify_cookie_expired,
    notify_sync_summary,
    save_telegram_settings,
    send_telegram_message,
    test_telegram_connection,
)

__all__ = [
    "get_telegram_settings",
    "save_telegram_settings",
    "send_telegram_message",
    "test_telegram_connection",
    "notify_sync_summary",
    "notify_cookie_expired",
]

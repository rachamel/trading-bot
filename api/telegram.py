# api/telegram.py — Telegram notifications with on/off from the terminal UI
# Never raises: a notification failure must never break the API flow.
import requests
import os
from store import get_setting

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def _safe_print(text):
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def enabled() -> bool:
    try:
        return get_setting("telegram_enabled", "true") == "true" and bool(TOKEN and CHAT_ID)
    except Exception:
        return False


def notify(text: str) -> bool:
    """Send a message only when notifications are toggled ON in the terminal."""
    try:
        if not enabled():
            _safe_print("[telegram] muted: " + text[:60])
            return False
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=20,
        )
        return r.status_code == 200
    except Exception as e:
        _safe_print(f"[telegram] error: {e}")
        return False

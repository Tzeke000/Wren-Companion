"""
Phase 83 — Windows notification tool.
SELF_ASSESSMENT: Tier 1 — Ava sends Windows toast notifications. She decides when and what is worth notifying.
She tracks response patterns to calibrate her notification frequency and importance threshold.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Notification log for bootstrap — Ava calibrates frequency from response patterns
_NOTIFICATION_LOG_PATH: Path | None = None
_NOTIFICATION_COUNT_TODAY: int = 0
_NOTIFICATION_DATE: str = ""


def _log_path(g: dict[str, Any]) -> Path:
    base = Path(g.get("BASE_DIR") or ".")
    return base / "state" / "notification_log.jsonl"


def _today_count(g: dict[str, Any]) -> int:
    """Count notifications sent today."""
    global _NOTIFICATION_COUNT_TODAY, _NOTIFICATION_DATE
    today = datetime.now().strftime("%Y-%m-%d")
    if _NOTIFICATION_DATE != today:
        _NOTIFICATION_DATE = today
        _NOTIFICATION_COUNT_TODAY = 0
        path = _log_path(g)
        if path.is_file():
            try:
                count = 0
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        entry = json.loads(line)
                        if entry.get("date") == today:
                            count += 1
                    except Exception:
                        pass
                _NOTIFICATION_COUNT_TODAY = count
            except Exception:
                pass
    return _NOTIFICATION_COUNT_TODAY


def send_notification(
    title: str,
    message: str,
    duration: int = 5,
    g: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Send a Windows desktop notification.
    title: max 50 chars. message: max 200 chars. duration: seconds.
    """
    if g is None:
        g = {}
    title = str(title or "")[:50]
    message = str(message or "")[:200]
    duration = max(1, min(30, int(duration or 5)))

    sent = False
    error = ""

    try:
        from plyer import notification as plyer_notification
        plyer_notification.notify(
            title=title,
            message=message,
            timeout=duration,
            app_name="Ava",
        )
        sent = True
    except Exception as e1:
        try:
            import subprocess
            # Windows toast via PowerShell as fallback
            _st = title.replace("'", "")
            _sm = message.replace("'", "")
            ps_parts = [
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
                " ContentType = WindowsRuntime] | Out-Null",
                "$template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02",
                "$toastXml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template)",
                f"$toastXml.GetElementsByTagName('text')[0].AppendChild($toastXml.CreateTextNode('{_st}')) | Out-Null",
                f"$toastXml.GetElementsByTagName('text')[1].AppendChild($toastXml.CreateTextNode('{_sm}')) | Out-Null",
                "$toast = [Windows.UI.Notifications.ToastNotification]::new($toastXml)",
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Ava').Show($toast)",
            ]
            ps_script = "; ".join(ps_parts)
            subprocess.run(
                ["powershell", "-Command", ps_script],
                capture_output=True, timeout=5,
            )
            sent = True
        except Exception as e2:
            error = f"plyer: {e1!s:.100}; ps: {e2!s:.100}"

    if sent:
        global _NOTIFICATION_COUNT_TODAY
        _NOTIFICATION_COUNT_TODAY += 1
        now = datetime.now()
        entry = {
            "ts": time.time(),
            "date": now.strftime("%Y-%m-%d"),
            "title": title,
            "message": message,
            "sent": True,
        }
        try:
            path = _log_path(g)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    return {
        "sent": sent,
        "title": title,
        "message": message,
        "today_count": _today_count(g),
        "error": error,
    }


def get_notification_count_today(g: dict[str, Any]) -> int:
    return _today_count(g)


# Tool registration shim
class NotificationTool:
    """Tier 1 tool — Ava decides when and what to notify."""
    name = "send_notification"
    tier = 1
    description = "Send a Windows desktop notification. Ava decides when to use this."

    def run(self, title: str, message: str, duration: int = 5, **kwargs: Any) -> dict[str, Any]:
        try:
            import avaagent as _av
            g = vars(_av)
        except Exception:
            g = {}
        return send_notification(title, message, duration, g)

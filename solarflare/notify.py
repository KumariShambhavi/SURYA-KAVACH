"""
notify.py — pluggable outbound alerting (email + webhook).

Adapters are enabled only when their environment variables are set, so the system
runs fine with no notifications configured. ``dispatch`` fans a message out to all
enabled channels and never raises (a failed notification must not break ingest).

Env:
  SOLARFLARE_WEBHOOK_URL                Slack-compatible incoming webhook
  SOLARFLARE_SMTP_HOST / _PORT / _USER / _PASS / _FROM / _TO   email (TO comma-sep)
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText

log = logging.getLogger("solarflare.notify")


def _webhook(text: str) -> None:
    url = os.getenv("SOLARFLARE_WEBHOOK_URL", "")
    if not url:
        return
    import httpx
    try:
        httpx.post(url, json={"text": text}, timeout=10)
    except Exception as e:  # noqa: BLE001
        log.warning("webhook notify failed: %s", e)


def _email(subject: str, text: str) -> None:
    host = os.getenv("SOLARFLARE_SMTP_HOST", "")
    to = os.getenv("SOLARFLARE_SMTP_TO", "")
    if not host or not to:
        return
    msg = MIMEText(text)
    msg["Subject"] = subject
    msg["From"] = os.getenv("SOLARFLARE_SMTP_FROM", "surya-kavach@localhost")
    msg["To"] = to
    try:
        with smtplib.SMTP(host, int(os.getenv("SOLARFLARE_SMTP_PORT", "587")), timeout=15) as s:
            s.starttls()
            user, pw = os.getenv("SOLARFLARE_SMTP_USER"), os.getenv("SOLARFLARE_SMTP_PASS")
            if user and pw:
                s.login(user, pw)
            s.sendmail(msg["From"], [a.strip() for a in to.split(",")], msg.as_string())
    except Exception as e:  # noqa: BLE001
        log.warning("email notify failed: %s", e)


def dispatch(subject: str, text: str) -> None:
    """Send to every configured channel. Safe: never raises."""
    _webhook(f"*{subject}*\n{text}")
    _email(subject, text)


def notify_new_flares(flares: list[dict], min_class: str = "M") -> int:
    """Notify about newly inserted flares at or above ``min_class``. Returns count sent."""
    order = "ABCMX"
    rank = order.index(min_class)
    notable = [f for f in flares
               if (f.get("flare_class") or " ")[0] in order
               and order.index((f["flare_class"])[0]) >= rank]
    if not notable:
        return 0
    lines = [f"- {f['flare_class']} at {f.get('peak_time_sxr') or f.get('onset_time')} "
             f"({f.get('status')})" for f in notable[:20]]
    dispatch(f"SURYA-KAVACH: {len(notable)} new ≥{min_class}-class flare(s)",
             "New significant solar flares detected:\n" + "\n".join(lines))
    return len(notable)

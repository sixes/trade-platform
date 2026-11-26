"""Operational alerts: always logged at ERROR, and emailed when SMTP credentials are set in the environment."""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

from backend.config import Settings

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send(self, subject: str, body: str) -> bool:
        log.error("ALERT %s - %s", subject, body.replace("\n", " | "))
        if not self.settings.get("alerts.email_enabled", True):
            return False
        user = os.environ.get("TRADE_PLAT_SMTP_USER", "")
        password = os.environ.get("TRADE_PLAT_SMTP_PASSWORD", "")
        if not user or not password:
            return False
        message = EmailMessage()
        message["Subject"] = f"[trade-plat] {subject}"
        message["From"] = user
        message["To"] = os.environ.get("TRADE_PLAT_ALERT_TO") or user
        message.set_content(body)
        host = os.environ.get("TRADE_PLAT_SMTP_HOST", "smtp.gmail.com")
        port = int(os.environ.get("TRADE_PLAT_SMTP_PORT", "465"))
        try:
            with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
                smtp.login(user, password)
                smtp.send_message(message)
            return True
        except Exception as exc:
            log.error("Alert email could not be sent: %s", exc)
            return False

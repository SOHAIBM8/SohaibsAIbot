"""
Minimal SMTP email dispatcher. Fixes docs/gap_audit_report.md P0 #3:
notification_preferences let an operator toggle email on and enter an
address, but nothing anywhere ever sent a message — the toggle was
inert. This is the smallest real implementation that closes that gap:
stdlib smtplib, no new dependency, STARTTLS-only (the overwhelmingly
common case — Gmail, SendGrid, Mailgun, local dev relays like MailHog
all support port 587 + STARTTLS); SMTP_SSL/port 465 is deliberately
not implemented (rule 8 — build the simple correct version first).

Credentials come from env vars only, matching every other credential-
adjacent module in this project — never hardcoded, never logged. Only
the outcome (sent/failed + recipient/subject) is logged, never the
message body or SMTP auth credentials, matching Stage 3 decision #8's
"no plaintext credential value in a log line, anywhere" discipline,
applied here to SMTP auth too.
"""

import os
import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from types import TracebackType
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)


class SMTPClient(Protocol):
    """Minimal shape EmailSender needs from an SMTP connection —
    narrow on purpose so a fake test double never has to implement the
    whole smtplib.SMTP surface (same pattern as LLMClient's
    AnthropicMessagesClient Protocol)."""

    def starttls(self) -> object: ...
    def login(self, user: str, password: str) -> object: ...
    def send_message(self, msg: EmailMessage) -> object: ...
    def __enter__(self) -> object: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
        /,
    ) -> None: ...


class EmailNotConfiguredError(RuntimeError):
    """Raised when send() is called but no SMTP host is configured —
    the caller must handle this (e.g. skip sending), never treat a
    missing configuration as a silent no-op that looks like success."""


class EmailSender:
    def __init__(
        self,
        host_env_var: str = "SMTP_HOST",
        port_env_var: str = "SMTP_PORT",
        username_env_var: str = "SMTP_USERNAME",
        password_env_var: str = "SMTP_PASSWORD",
        from_address_env_var: str = "SMTP_FROM_ADDRESS",
        smtp_client_factory: Callable[[str, int], SMTPClient] | None = None,
    ):
        self.host_env_var = host_env_var
        self.port_env_var = port_env_var
        self.username_env_var = username_env_var
        self.password_env_var = password_env_var
        self.from_address_env_var = from_address_env_var
        # None in production (a real smtplib.SMTP is built lazily,
        # only inside send() — never at construction time, so building
        # an EmailSender is always safe even with no SMTP_HOST set);
        # always non-None in tests via injection. Same lazy-real-client
        # shape as LLMClient's anthropic_client.
        self._smtp_client_factory = smtp_client_factory

    def is_configured(self) -> bool:
        return bool(os.environ.get(self.host_env_var))

    def send(self, to_address: str, subject: str, body: str) -> None:
        host = os.environ.get(self.host_env_var)
        if not host:
            raise EmailNotConfiguredError(
                f"no SMTP host configured (env var {self.host_env_var} is unset)"
            )
        port = int(os.environ.get(self.port_env_var, "587"))
        username = os.environ.get(self.username_env_var)
        password = os.environ.get(self.password_env_var)
        from_address = os.environ.get(self.from_address_env_var) or username or "noreply@localhost"

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = from_address
        message["To"] = to_address
        message.set_content(body)

        client_factory = self._smtp_client_factory or _real_smtp_client
        client = client_factory(host, port)
        try:
            with client:
                if username and password:
                    client.starttls()
                    client.login(username, password)
                client.send_message(message)
        except Exception:
            logger.exception(
                "email_notification_send_failed", to_address=to_address, subject=subject
            )
            raise
        logger.info("email_notification_sent", to_address=to_address, subject=subject)


def _real_smtp_client(host: str, port: int) -> SMTPClient:
    return smtplib.SMTP(host, port, timeout=10)

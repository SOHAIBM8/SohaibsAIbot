"""
No real network/SMTP server in the standard suite — a scripted fake
client, same pattern as LLMClient's fake Anthropic client
(ScriptedAnthropicMessages).
"""

import pytest

from core.notifications.email_sender import EmailNotConfiguredError, EmailSender

_ENV_PREFIX = "TEST_EMAIL_SENDER_"


class _FakeSMTPClient:
    def __init__(self, host: str, port: int, raise_on_send: bool = False):
        self.host = host
        self.port = port
        self.raise_on_send = raise_on_send
        self.started_tls = False
        self.logged_in_as: tuple[str, str] | None = None
        self.sent_messages: list = []
        self.entered = False
        self.exited = False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in_as = (user, password)

    def send_message(self, msg):
        if self.raise_on_send:
            raise ConnectionError("simulated SMTP failure")
        self.sent_messages.append(msg)

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.exited = True


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for suffix in ("HOST", "PORT", "USERNAME", "PASSWORD", "FROM_ADDRESS"):
        monkeypatch.delenv(f"{_ENV_PREFIX}{suffix}", raising=False)


def make_sender(fake_client: _FakeSMTPClient | None = None) -> EmailSender:
    factory = (lambda host, port: fake_client) if fake_client is not None else None
    return EmailSender(
        host_env_var=f"{_ENV_PREFIX}HOST",
        port_env_var=f"{_ENV_PREFIX}PORT",
        username_env_var=f"{_ENV_PREFIX}USERNAME",
        password_env_var=f"{_ENV_PREFIX}PASSWORD",
        from_address_env_var=f"{_ENV_PREFIX}FROM_ADDRESS",
        smtp_client_factory=factory,
    )


def test_is_configured_false_when_no_host_env_var_set():
    assert make_sender().is_configured() is False


def test_is_configured_true_when_host_env_var_set(monkeypatch):
    monkeypatch.setenv(f"{_ENV_PREFIX}HOST", "smtp.example.com")
    assert make_sender().is_configured() is True


def test_send_raises_not_configured_when_no_host_set():
    sender = make_sender()
    with pytest.raises(EmailNotConfiguredError):
        sender.send("ops@example.com", "subject", "body")


def test_send_delivers_message_via_the_injected_client(monkeypatch):
    monkeypatch.setenv(f"{_ENV_PREFIX}HOST", "smtp.example.com")
    monkeypatch.setenv(f"{_ENV_PREFIX}PORT", "2525")
    fake = _FakeSMTPClient("smtp.example.com", 2525)
    sender = make_sender(fake)

    sender.send("ops@example.com", "Kill switch engaged", "Engaged by devops: manual halt")

    assert fake.entered is True
    assert fake.exited is True
    assert len(fake.sent_messages) == 1
    sent = fake.sent_messages[0]
    assert sent["To"] == "ops@example.com"
    assert sent["Subject"] == "Kill switch engaged"
    assert sent.get_content().strip() == "Engaged by devops: manual halt"


def test_send_uses_starttls_and_login_when_credentials_are_set(monkeypatch):
    monkeypatch.setenv(f"{_ENV_PREFIX}HOST", "smtp.example.com")
    monkeypatch.setenv(f"{_ENV_PREFIX}USERNAME", "smtp-user")
    monkeypatch.setenv(f"{_ENV_PREFIX}PASSWORD", "smtp-pass")
    fake = _FakeSMTPClient("smtp.example.com", 587)
    sender = make_sender(fake)

    sender.send("ops@example.com", "subject", "body")

    assert fake.started_tls is True
    assert fake.logged_in_as == ("smtp-user", "smtp-pass")


def test_send_skips_login_when_no_credentials_set(monkeypatch):
    monkeypatch.setenv(f"{_ENV_PREFIX}HOST", "smtp.example.com")
    fake = _FakeSMTPClient("smtp.example.com", 587)
    sender = make_sender(fake)

    sender.send("ops@example.com", "subject", "body")

    assert fake.started_tls is False
    assert fake.logged_in_as is None


def test_send_uses_from_address_env_var_when_set(monkeypatch):
    monkeypatch.setenv(f"{_ENV_PREFIX}HOST", "smtp.example.com")
    monkeypatch.setenv(f"{_ENV_PREFIX}FROM_ADDRESS", "alerts@mytradingapp.com")
    fake = _FakeSMTPClient("smtp.example.com", 587)
    sender = make_sender(fake)

    sender.send("ops@example.com", "subject", "body")

    assert fake.sent_messages[0]["From"] == "alerts@mytradingapp.com"


def test_send_propagates_failures_from_the_smtp_client(monkeypatch):
    monkeypatch.setenv(f"{_ENV_PREFIX}HOST", "smtp.example.com")
    fake = _FakeSMTPClient("smtp.example.com", 587, raise_on_send=True)
    sender = make_sender(fake)

    with pytest.raises(ConnectionError):
        sender.send("ops@example.com", "subject", "body")

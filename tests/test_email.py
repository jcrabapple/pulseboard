"""Tests for the SMTP email notification channel.

These tests intentionally avoid the real network. ``smtplib.SMTP`` is
patched through ``monkeypatch`` with a fake class that records every
method call, mirroring the MockTransport pattern used by the HTTP
channel tests in ``test_notifications.py``.
"""

from __future__ import annotations

import smtplib
from datetime import datetime, timezone
from email import message_from_string
from email.utils import getaddresses

import pytest

from pulseboard.alerting import Alert, AlertType
from pulseboard.models import (
    ChannelType,
    CheckResult,
    NotificationChannel,
    Status,
)
from pulseboard.notifications import (
    DEFAULT_SMTP_PORT,
    SMTP_TIMEOUT_SECONDS,
    NotificationDispatcher,
    render_email_payload,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _make_alert(
    service: str = "GitHub",
    status: Status = Status.DOWN,
    alert_type: str = AlertType.DOWN,
    latency: float = 150.0,
    status_code=None,
    error: str | None = "HTTP 500",
) -> Alert:
    """Build a synthetic Alert with sensible defaults."""
    result = CheckResult(
        service_name=service,
        timestamp=datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc),
        status=status,
        latency_ms=latency,
        status_code=status_code,
        error=error,
    )
    return Alert(
        service_name=service,
        alert_type=alert_type,
        result=result,
        message=f"\U0001f534 {service} went DOWN: {error}",
    )


def _email_channel(
    name: str = "ops-email",
    host: str = "smtp.example.com",
    port: int | None = None,
    username: str | None = None,
    password: str | None = None,
    use_tls: bool = True,
    from_addr: str = "pulseboard@example.com",
    to_addrs: list[str] | None = None,
    subject_prefix: str = "[PulseBoard]",
) -> NotificationChannel:
    return NotificationChannel(
        name=name,
        channel_type=ChannelType.EMAIL,
        smtp_host=host,
        smtp_port=port,
        smtp_username=username,
        smtp_password=password,
        smtp_use_tls=use_tls,
        smtp_from_addr=from_addr,
        smtp_to_addrs=to_addrs or ["ops@example.com"],
        smtp_subject_prefix=subject_prefix,
    )


class _FakeSMTP:
    """Stand-in for smtplib.SMTP that records every interaction.

    Returned via a factory so each test gets a fresh instance. Supports
    the context-manager protocol and the small subset of methods that
    ``_smtp_send`` calls.
    """

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ehlo_calls = 0
        self.starttls_called = False
        self.tls_context = None
        self.login_args: tuple | None = None
        self.sent_message: str | None = None
        self.quit_called = False
        # Append AFTER all attributes exist so __init__ failure doesn't
        # leave a half-built instance in the class list.
        _FakeSMTP.instances.append(self)

    # Context-manager support -- _smtp_send uses `with smtp_class(...) as smtp:`
    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.quit_called = True

    def ehlo(self) -> tuple[int, bytes]:
        self.ehlo_calls += 1
        return (250, b"OK")

    def starttls(self, context=None) -> tuple[int, bytes]:
        self.starttls_called = True
        self.tls_context = context
        return (220, b"Ready to start TLS")

    def login(self, user, password) -> tuple[int, bytes]:
        self.login_args = (user, password)
        return (235, b"Authenticated")

    def send_message(self, msg) -> None:
        # serialize the message exactly the way smtplib will hand it to
        # the wire -- a flattened str.
        self.sent_message = msg.as_string()

    # Quack like the real class so any future tests can introspect.
    def quit(self) -> None:
        self.quit_called = True


@pytest.fixture
def fake_smtp(monkeypatch):
    """Reset the fake class list and patch smtplib.SMTP to _FakeSMTP."""
    _FakeSMTP.instances = []
    monkeypatch.setattr("pulseboard.notifications.smtplib.SMTP", _FakeSMTP)
    return _FakeSMTP


# ---------------------------------------------------------------------------
# Payload renderer tests -- pure functions, no I/O.
# ---------------------------------------------------------------------------


class TestEmailRenderer:
    def test_returns_email_message_instance(self):
        msg = render_email_payload(_make_alert(), _email_channel())
        # An EmailMessage round-trips through email.message_from_string.
        # Use that to assert the structure without depending on internal
        # attributes that vary by Python version.
        raw = msg.as_string()
        parsed = message_from_string(raw)
        assert parsed["Subject"]
        assert parsed["From"]
        assert parsed["To"]

    def test_subject_includes_prefix_and_title(self):
        msg = render_email_payload(_make_alert(), _email_channel())
        parsed = message_from_string(msg.as_string())
        assert parsed["Subject"].startswith("[PulseBoard]")
        assert "GitHub" in parsed["Subject"]
        assert "DOWN" in parsed["Subject"]

    def test_subject_prefix_is_configurable(self):
        msg = render_email_payload(
            _make_alert(), _email_channel(subject_prefix="[ALERT]")
        )
        parsed = message_from_string(msg.as_string())
        assert parsed["Subject"].startswith("[ALERT]")

    def test_from_header_uses_channel_from_addr(self):
        msg = render_email_payload(
            _make_alert(),
            _email_channel(from_addr="alerts@corp.example.com"),
        )
        parsed = message_from_string(msg.as_string())
        assert parsed["From"] == "alerts@corp.example.com"

    def test_to_header_lists_all_recipients(self):
        msg = render_email_payload(
            _make_alert(),
            _email_channel(to_addrs=["a@example.com", "b@example.com", "c@example.com"]),
        )
        parsed = message_from_string(msg.as_string())
        # Use getaddresses to robustly parse the To header (handles
        # "Name <addr>" shapes, extra whitespace, etc).
        addrs = [a for _, a in getaddresses([parsed["To"]])]
        assert addrs == ["a@example.com", "b@example.com", "c@example.com"]

    def test_x_pulseboard_headers_present(self):
        msg = render_email_payload(_make_alert(), _email_channel())
        parsed = message_from_string(msg.as_string())
        assert parsed["X-PulseBoard-Alert"] == AlertType.DOWN
        assert parsed["X-PulseBoard-Service"] == "GitHub"

    def test_x_pulseboard_alert_type_matches_recovery(self):
        alert = _make_alert(
            status=Status.UP, alert_type=AlertType.RECOVERY, error=None
        )
        msg = render_email_payload(alert, _email_channel())
        parsed = message_from_string(msg.as_string())
        assert parsed["X-PulseBoard-Alert"] == AlertType.RECOVERY

    def test_has_plain_text_body(self):
        msg = render_email_payload(_make_alert(), _email_channel())
        # The plain-text part is the first payload added.
        text = msg.get_body(preferencelist=("plain",))
        assert text is not None
        body = text.get_content()
        assert "GitHub" in body
        assert "PulseBoard" in body

    def test_has_html_alternative(self):
        msg = render_email_payload(_make_alert(), _email_channel())
        html = msg.get_body(preferencelist=("html",))
        assert html is not None
        body = html.get_content()
        assert "<html" in body.lower()
        assert "GitHub" in body

    def test_html_color_matches_status(self):
        # DOWN should be red (E74C3C). Pull the color out of the h2 style.
        msg = render_email_payload(_make_alert(status=Status.DOWN), _email_channel())
        html = msg.get_body(preferencelist=("html",)).get_content()
        assert "#E74C3C" in html

    def test_html_color_for_recovery_is_green(self):
        msg = render_email_payload(
            _make_alert(status=Status.UP, alert_type=AlertType.RECOVERY),
            _email_channel(),
        )
        html = msg.get_body(preferencelist=("html",)).get_content()
        assert "#2ECC71" in html

    def test_html_escapes_special_chars(self):
        # If a service name contains HTML metacharacters they must be
        # entity-escaped in the HTML part, otherwise they break the
        # layout and (worse) can become an XSS vector for a malicious
        # service name.
        alert = _make_alert(service="<script>alert(1)</script>")
        msg = render_email_payload(alert, _email_channel())
        html = msg.get_body(preferencelist=("html",)).get_content()
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestEmailValidation:
    def test_validate_requires_smtp_host(self):
        ch = NotificationChannel(
            name="x",
            channel_type=ChannelType.EMAIL,
            smtp_from_addr="a@b.com",
            smtp_to_addrs=["c@d.com"],
        )
        with pytest.raises(ValueError, match="smtp_host"):
            ch.validate()

    def test_validate_requires_from_addr(self):
        ch = NotificationChannel(
            name="x",
            channel_type=ChannelType.EMAIL,
            smtp_host="smtp.example.com",
            smtp_to_addrs=["c@d.com"],
        )
        with pytest.raises(ValueError, match="smtp_from_addr"):
            ch.validate()

    def test_validate_requires_at_least_one_recipient(self):
        ch = NotificationChannel(
            name="x",
            channel_type=ChannelType.EMAIL,
            smtp_host="smtp.example.com",
            smtp_from_addr="a@b.com",
            smtp_to_addrs=[],
        )
        with pytest.raises(ValueError, match="smtp_to_addrs"):
            ch.validate()

    def test_validate_rejects_invalid_port(self):
        ch = NotificationChannel(
            name="x",
            channel_type=ChannelType.EMAIL,
            smtp_host="smtp.example.com",
            smtp_port=999999,  # out of range
            smtp_from_addr="a@b.com",
            smtp_to_addrs=["c@d.com"],
        )
        with pytest.raises(ValueError, match="smtp_port"):
            ch.validate()

    def test_validate_accepts_minimal_valid_config(self):
        ch = NotificationChannel(
            name="x",
            channel_type=ChannelType.EMAIL,
            smtp_host="smtp.example.com",
            smtp_from_addr="a@b.com",
            smtp_to_addrs=["c@d.com"],
        )
        ch.validate()  # no exception

    def test_validate_accepts_explicit_port(self):
        ch = NotificationChannel(
            name="x",
            channel_type=ChannelType.EMAIL,
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_from_addr="a@b.com",
            smtp_to_addrs=["c@d.com"],
        )
        ch.validate()


# ---------------------------------------------------------------------------
# from_config tests
# ---------------------------------------------------------------------------


class TestFromConfigEmail:
    def test_builds_email_channel_from_dict(self):
        d = NotificationDispatcher.from_config([
            {
                "name": "ops",
                "type": "email",
                "smtp_host": "smtp.example.com",
                "smtp_from_addr": "pulse@example.com",
                "smtp_to_addrs": ["oncall@example.com"],
            }
        ])
        assert len(d.channels) == 1
        ch = d.channels[0]
        assert ch.channel_type is ChannelType.EMAIL
        assert ch.smtp_host == "smtp.example.com"
        assert ch.smtp_to_addrs == ["oncall@example.com"]
        assert ch.smtp_use_tls is True  # default
        assert ch.smtp_port is None
        assert ch.smtp_subject_prefix == "[PulseBoard]"

    def test_parses_full_email_config(self):
        d = NotificationDispatcher.from_config([
            {
                "name": "ops",
                "type": "email",
                "smtp_host": "smtp.gmail.com",
                "smtp_port": 587,
                "smtp_username": "alerts@gmail.com",
                "smtp_password": "app-password",
                "smtp_use_tls": False,
                "smtp_from_addr": "alerts@gmail.com",
                "smtp_to_addrs": ["a@x.com", "b@y.com"],
                "smtp_subject_prefix": "[ONCALL]",
            }
        ])
        ch = d.channels[0]
        assert ch.smtp_port == 587
        assert ch.smtp_username == "alerts@gmail.com"
        assert ch.smtp_password == "app-password"
        assert ch.smtp_use_tls is False
        assert ch.smtp_to_addrs == ["a@x.com", "b@y.com"]
        assert ch.smtp_subject_prefix == "[ONCALL]"

    def test_missing_smtp_host_in_config_raises(self):
        with pytest.raises(ValueError, match="smtp_host"):
            NotificationDispatcher.from_config([
                {
                    "name": "bad",
                    "type": "email",
                    "smtp_from_addr": "a@b.com",
                    "smtp_to_addrs": ["c@d.com"],
                }
            ])

    def test_empty_to_addrs_in_config_raises(self):
        with pytest.raises(ValueError, match="smtp_to_addrs"):
            NotificationDispatcher.from_config([
                {
                    "name": "bad",
                    "type": "email",
                    "smtp_host": "smtp.example.com",
                    "smtp_from_addr": "a@b.com",
                    "smtp_to_addrs": [],
                }
            ])

    def test_invalid_port_in_config_raises(self):
        with pytest.raises(ValueError):
            NotificationDispatcher.from_config([
                {
                    "name": "bad",
                    "type": "email",
                    "smtp_host": "smtp.example.com",
                    "smtp_port": "not-a-number",
                    "smtp_from_addr": "a@b.com",
                    "smtp_to_addrs": ["c@d.com"],
                }
            ])

    def test_non_list_to_addrs_in_config_raises(self):
        with pytest.raises(ValueError):
            NotificationDispatcher.from_config([
                {
                    "name": "bad",
                    "type": "email",
                    "smtp_host": "smtp.example.com",
                    "smtp_from_addr": "a@b.com",
                    "smtp_to_addrs": "c@d.com",  # should be a list
                }
            ])


# ---------------------------------------------------------------------------
# Dispatcher + SMTP send tests
# ---------------------------------------------------------------------------


class TestDispatchEmail:
    def test_dispatch_sends_email_to_smtp(self, fake_smtp):
        d = NotificationDispatcher(channels=[_email_channel()])
        results = d.dispatch_sync(_make_alert())
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].status_code == 250  # SMTP "accepted"
        assert fake_smtp.instances and len(fake_smtp.instances) == 1
        smtp = fake_smtp.instances[0]
        assert smtp.host == "smtp.example.com"
        assert smtp.sent_message is not None
        assert "GitHub" in smtp.sent_message

    def test_dispatch_uses_default_port_when_unset(self, fake_smtp):
        d = NotificationDispatcher(channels=[_email_channel()])  # no smtp_port
        d.dispatch_sync(_make_alert())
        assert fake_smtp.instances[0].port == DEFAULT_SMTP_PORT
        assert fake_smtp.instances[0].timeout == SMTP_TIMEOUT_SECONDS

    def test_dispatch_uses_explicit_port(self, fake_smtp):
        d = NotificationDispatcher(channels=[_email_channel(port=2525)])
        d.dispatch_sync(_make_alert())
        assert fake_smtp.instances[0].port == 2525

    def test_dispatch_starts_tls_by_default(self, fake_smtp):
        d = NotificationDispatcher(channels=[_email_channel()])
        d.dispatch_sync(_make_alert())
        smtp = fake_smtp.instances[0]
        assert smtp.starttls_called is True
        # STARTTLS must run BEFORE login or auth would be in cleartext.
        # We can't trivially assert ordering from a list, but ehlo() is
        # called twice (once before, once after STARTTLS) -- that's the
        # easiest verifiable signal.
        assert smtp.ehlo_calls == 2

    def test_dispatch_skips_tls_when_disabled(self, fake_smtp):
        d = NotificationDispatcher(channels=[_email_channel(use_tls=False)])
        d.dispatch_sync(_make_alert())
        smtp = fake_smtp.instances[0]
        assert smtp.starttls_called is False
        assert smtp.ehlo_calls == 1  # just the one initial EHLO

    def test_dispatch_logs_in_when_username_provided(self, fake_smtp):
        d = NotificationDispatcher(
            channels=[_email_channel(username="alerts", password="hunter2")]
        )
        d.dispatch_sync(_make_alert())
        smtp = fake_smtp.instances[0]
        assert smtp.login_args == ("alerts", "hunter2")

    def test_dispatch_skips_login_when_no_username(self, fake_smtp):
        d = NotificationDispatcher(channels=[_email_channel()])
        d.dispatch_sync(_make_alert())
        assert fake_smtp.instances[0].login_args is None

    def test_dispatch_quits_connection(self, fake_smtp):
        d = NotificationDispatcher(channels=[_email_channel()])
        d.dispatch_sync(_make_alert())
        assert fake_smtp.instances[0].quit_called is True

    def test_dispatch_failure_surfaces_error(self, monkeypatch):
        """If SMTP auth fails, the dispatch returns a failure with the
        exception name in the error string so the user can diagnose.
        """
        class _ExplodingSMTP:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return None

            def ehlo(self):
                return (250, b"OK")

            def starttls(self, context=None):
                return (220, b"OK")

            def login(self, *a):
                raise smtplib.SMTPAuthenticationError(535, b"auth failed")

        monkeypatch.setattr(
            "pulseboard.notifications.smtplib.SMTP", _ExplodingSMTP
        )
        # _email_channel() defaults to no username -- auth is skipped
        # in that case. We need a real username so login() is invoked.
        d = NotificationDispatcher(
            channels=[_email_channel(username="alerts", password="wrong")]
        )
        results = d.dispatch_sync(_make_alert())
        assert len(results) == 1
        assert results[0].success is False
        assert "SMTPAuthenticationError" in (results[0].error or "")
        assert results[0].status_code is None

    def test_dispatch_connection_error_surfaces(self, monkeypatch):
        class _RefusingSMTP:
            def __init__(self, *a, **kw):
                raise OSError("connection refused")

        monkeypatch.setattr(
            "pulseboard.notifications.smtplib.SMTP", _RefusingSMTP
        )
        d = NotificationDispatcher(channels=[_email_channel()])
        results = d.dispatch_sync(_make_alert())
        assert results[0].success is False
        assert "OSError" in (results[0].error or "")
        assert "connection refused" in (results[0].error or "")

    def test_dispatch_fans_out_to_email_and_http(
        self, fake_smtp, monkeypatch
    ):
        """Email + Slack firing concurrently should each see a real send.

        Patches the SMTP class AND the httpx async factory so both
        channels run. The EmailMessage in the SMTP instance should
        contain the same service name as the Slack payload.
        """
        import httpx as _httpx

        # Capture every HTTP request that flows through AsyncClient.
        http_requests: list = []

        def _handler(request: _httpx.Request) -> _httpx.Response:
            http_requests.append(request)
            return _httpx.Response(200, json={"ok": True})

        def _factory(*a, **kw):
            if "transport" in kw:
                return _orig_async(*a, **kw)
            return _orig_async(*a, transport=_httpx.MockTransport(_handler), **kw)

        _orig_async = _httpx.AsyncClient
        monkeypatch.setattr(
            "pulseboard.notifications.httpx.AsyncClient", _factory
        )

        slack = NotificationChannel(
            name="slack",
            channel_type=ChannelType.SLACK,
            webhook_url="https://hooks.slack.com/x",
        )
        email = _email_channel(name="email")
        d = NotificationDispatcher(channels=[slack, email])
        results = d.dispatch_sync(_make_alert())
        assert len(results) == 2
        assert all(r.success for r in results)
        # HTTP got a request
        assert len(http_requests) == 1
        # SMTP got a connection
        assert len(_FakeSMTP.instances) == 1
        assert "GitHub" in (_FakeSMTP.instances[0].sent_message or "")

    def test_dispatch_routes_per_service_alert_channels(self, fake_smtp):
        from pulseboard.models import ServiceConfig, ServiceType

        d = NotificationDispatcher(
            channels=[_email_channel("a"), _email_channel("b")]
        )
        svc = ServiceConfig(
            name="svc", url="x", service_type=ServiceType.HTTP,
            alert_channels=["b"],
        )
        results = d.dispatch_sync(_make_alert(), svc)
        # Only "b" fires.
        assert len(results) == 1
        assert results[0].channel.name == "b"
        # And only that one SMTP connection was opened.
        assert len(fake_smtp.instances) == 1

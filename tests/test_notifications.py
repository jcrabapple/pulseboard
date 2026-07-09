"""Tests for the notification channel dispatchers.

Uses ``httpx.MockTransport`` rather than respx so the tests run in any
environment where httpx itself is available -- no extra dependency.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from pulseboard.alerting import Alert, AlertType
from pulseboard.models import (
    ChannelType,
    CheckResult,
    NotificationChannel,
    ServiceConfig,
    ServiceType,
    Status,
)
from pulseboard.notifications import (
    ChannelSendResult,
    NotificationDispatcher,
    render_discord_payload,
    render_slack_payload,
    render_telegram_payload,
    render_webhook_payload,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_alert(
    service: str = "GitHub",
    status: Status = Status.DOWN,
    alert_type: str = AlertType.DOWN,
    latency: float = 150.0,
    status_code=None,
    error: str | None = "HTTP 500",
    message: str | None = None,
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
        message=message or f"\U0001f534 {service} went DOWN: {error}",
    )


class _Recorder:
    """Collect every request that flows through a MockTransport."""

    def __init__(self, status_code: int = 200, json_response=None):
        self.requests: list[httpx.Request] = []
        self.status_code = status_code
        self.json_response = json_response or {"ok": True}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code, json=self.json_response)

    def make_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def recorder() -> _Recorder:
    return _Recorder()


def _install_async_factory(monkeypatch, factory) -> None:
    """Install a factory as pulseboard.notifications.httpx.AsyncClient.

    The factory must capture the real AsyncClient class up-front; if it
    re-resolves ``httpx.AsyncClient`` through the patched module, it
    recurses infinitely.
    """
    monkeypatch.setattr("pulseboard.notifications.httpx.AsyncClient", factory)


@pytest.fixture
def mock_async(recorder: _Recorder, monkeypatch):
    """Route every httpx.AsyncClient call in pulseboard.notifications through the recorder."""
    import httpx as _httpx

    _orig = _httpx.AsyncClient

    def _factory(*a, **kw):
        if "transport" in kw:
            return _orig(*a, **kw)
        return _orig(*a, transport=recorder.make_transport(), **kw)

    _install_async_factory(monkeypatch, _factory)
    return recorder

# ---------------------------------------------------------------------------
# Payload renderer tests -- pure functions, no I/O.
# ---------------------------------------------------------------------------


class TestSlackRenderer:
    def test_has_text_fallback(self):
        payload = render_slack_payload(_make_alert())
        assert "text" in payload
        assert "GitHub" in payload["text"]

    def test_has_attachment_with_color(self):
        payload = render_slack_payload(_make_alert(status=Status.DOWN))
        assert "attachments" in payload
        att = payload["attachments"][0]
        assert att["color"] == "#E74C3C"

    def test_attachment_color_changes_with_status(self):
        green = render_slack_payload(
            _make_alert(status=Status.UP, alert_type=AlertType.RECOVERY)
        )["attachments"][0]["color"]
        amber = render_slack_payload(
            _make_alert(status=Status.DEGRADED, alert_type=AlertType.DEGRADED)
        )["attachments"][0]["color"]
        assert green == "#2ECC71"
        assert amber == "#F1C40F"

    def test_includes_timestamp(self):
        payload = render_slack_payload(_make_alert())
        assert isinstance(payload["attachments"][0]["ts"], int)


class TestDiscordRenderer:
    def test_has_content_and_embed(self):
        payload = render_discord_payload(_make_alert())
        assert "content" in payload
        assert "embeds" in payload
        assert payload["embeds"][0]["title"]

    def test_embed_color_matches_status(self):
        payload = render_discord_payload(_make_alert(status=Status.DOWN))
        assert payload["embeds"][0]["color"] == 0xE74C3C

    def test_status_code_field_present_when_status_code_set(self):
        payload = render_discord_payload(_make_alert(status_code=503))
        names = [f["name"] for f in payload["embeds"][0]["fields"]]
        assert "Status code" in names
        assert "503" in [f["value"] for f in payload["embeds"][0]["fields"]]

    def test_status_code_field_absent_when_none(self):
        payload = render_discord_payload(_make_alert(status_code=None))
        names = [f["name"] for f in payload["embeds"][0]["fields"]]
        assert "Status code" not in names


class TestTelegramRenderer:
    def test_markdown_text(self):
        payload = render_telegram_payload(_make_alert())
        assert payload["parse_mode"] == "Markdown"
        assert payload["text"].startswith("*" + "\U0001f534")
        assert "GitHub" in payload["text"]
        assert "PulseBoard" in payload["text"]

    def test_disables_web_page_preview(self):
        payload = render_telegram_payload(_make_alert())
        assert payload["disable_web_page_preview"] is True


class TestWebhookRenderer:
    def test_legacy_shape_preserved(self):
        alert = _make_alert()
        payload = render_webhook_payload(alert)
        expected_keys = {
            "service_name", "type", "message", "timestamp", "latency_ms", "error",
        }
        assert set(payload.keys()) == expected_keys
        assert payload["service_name"] == "GitHub"
        assert payload["type"] == "down"
# ---------------------------------------------------------------------------
# NotificationChannel dataclass & validation
# ---------------------------------------------------------------------------


class TestNotificationChannel:
    def test_string_channel_type_normalized_to_enum(self):
        ch = NotificationChannel(name="x", channel_type="slack")
        assert ch.channel_type is ChannelType.SLACK

    def test_validate_slack_requires_webhook_url(self):
        ch = NotificationChannel(name="x", channel_type=ChannelType.SLACK)
        with pytest.raises(ValueError, match="requires 'webhook_url'"):
            ch.validate()

    def test_validate_slack_webhook_must_be_http(self):
        ch = NotificationChannel(
            name="x", channel_type=ChannelType.SLACK, webhook_url="ftp://nope"
        )
        with pytest.raises(ValueError, match="must start with http"):
            ch.validate()

    def test_validate_telegram_requires_token_and_chat_id(self):
        ch = NotificationChannel(
            name="x", channel_type=ChannelType.TELEGRAM,
            webhook_url="https://example.com",
        )
        with pytest.raises(ValueError, match="requires 'telegram_token'"):
            ch.validate()
        ch.telegram_token = "tok"
        with pytest.raises(ValueError, match="requires 'telegram_chat_id'"):
            ch.validate()

    def test_validate_telegram_ok_with_both(self):
        ch = NotificationChannel(
            name="x", channel_type=ChannelType.TELEGRAM,
            telegram_token="tok", telegram_chat_id="123",
        )
        ch.validate()

    def test_validate_discord_requires_webhook(self):
        ch = NotificationChannel(name="x", channel_type=ChannelType.DISCORD)
        with pytest.raises(ValueError):
            ch.validate()


# ---------------------------------------------------------------------------
# Dispatcher tests -- exercise the async send path with MockTransport
# ---------------------------------------------------------------------------


def _slack_channel(name: str = "slack-ops") -> NotificationChannel:
    return NotificationChannel(
        name=name, channel_type=ChannelType.SLACK,
        webhook_url="https://hooks.slack.com/services/T0/B0/XXX",
    )


def _discord_channel(name: str = "discord-ops") -> NotificationChannel:
    return NotificationChannel(
        name=name, channel_type=ChannelType.DISCORD,
        webhook_url="https://discord.com/api/webhooks/1/abc",
    )


def _telegram_channel(name: str = "tg-ops") -> NotificationChannel:
    return NotificationChannel(
        name=name, channel_type=ChannelType.TELEGRAM,
        telegram_token="123:abc", telegram_chat_id="-100123",
    )


def _webhook_channel(name: str = "raw") -> NotificationChannel:
    return NotificationChannel(
        name=name, channel_type=ChannelType.WEBHOOK,
        webhook_url="https://example.com/hook",
    )


def _service(name: str = "svc", alert_channels=None, alert_webhook=None) -> ServiceConfig:
    return ServiceConfig(
        name=name, url="https://example.com", service_type=ServiceType.HTTP,
        alert_channels=alert_channels or [],
        alert_webhook=alert_webhook,
    )


class TestDispatcherRouting:
    def test_from_config_empty(self):
        d = NotificationDispatcher.from_config(None)
        assert d.channels == []
        d2 = NotificationDispatcher.from_config([])
        assert d2.channels == []

    def test_from_config_builds_slack(self):
        d = NotificationDispatcher.from_config(
            [{"name": "ops", "type": "slack", "webhook_url": "https://hooks.slack.com/x"}]
        )
        assert len(d.channels) == 1
        assert d.channels[0].channel_type is ChannelType.SLACK
        assert "ops" in d._by_name

    def test_from_config_propagates_validation_error(self):
        with pytest.raises(ValueError, match="requires 'webhook_url'"):
            NotificationDispatcher.from_config(
                [{"name": "bad", "type": "slack"}]
            )

    def test_resolve_all_channels_when_no_service(self):
        d = NotificationDispatcher(channels=[_slack_channel(), _discord_channel()])
        assert len(d.resolve_channels(None)) == 2

    def test_resolve_all_channels_when_service_has_no_override(self):
        d = NotificationDispatcher(channels=[_slack_channel(), _discord_channel()])
        assert len(d.resolve_channels(_service())) == 2

    def test_resolve_filters_to_alert_channels(self):
        d = NotificationDispatcher(channels=[_slack_channel("a"), _discord_channel("b")])
        svc = _service(alert_channels=["a"])
        targets = d.resolve_channels(svc)
        assert [c.name for c in targets] == ["a"]

    def test_resolve_silently_skips_unknown_channel_name(self):
        # Config bug shouldn't crash the watcher loop.
        d = NotificationDispatcher(channels=[_slack_channel("a")])
        svc = _service(alert_channels=["a", "nonexistent"])
        targets = d.resolve_channels(svc)
        assert [c.name for c in targets] == ["a"]

    def test_resolve_synthesizes_legacy_alert_webhook(self):
        d = NotificationDispatcher(channels=[_slack_channel()])
        svc = _service(alert_webhook="https://legacy.example/hook")
        targets = d.resolve_channels(svc)
        # Should have the slack channel AND the synthesized webhook.
        assert len(targets) == 2
        assert any(c.channel_type == ChannelType.WEBHOOK for c in targets)

    def test_resolve_does_not_duplicate_matching_legacy_webhook(self):
        d = NotificationDispatcher(
            channels=[_webhook_channel("raw",)]
        )
        # Service has the same webhook URL the channel uses -- no duplication.
        d.channels[0] = _webhook_channel("raw")
        d.channels[0].webhook_url = "https://legacy.example/hook"
        svc = _service(alert_webhook="https://legacy.example/hook")
        targets = d.resolve_channels(svc)
        assert len(targets) == 1


class TestDispatch:
    def test_dispatch_with_no_targets_returns_empty(self, mock_async):
        d = NotificationDispatcher(channels=[])
        result = d.dispatch_sync(_make_alert())
        assert result == []
        # Nothing should have been sent.
        assert mock_async.requests == []

    def test_dispatch_slack_posts_to_webhook(self, mock_async):
        d = NotificationDispatcher(channels=[_slack_channel()])
        results = d.dispatch_sync(_make_alert())
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].status_code == 200
        assert len(mock_async.requests) == 1
        # Body should be the slack payload.
        body = json.loads(mock_async.requests[0].content)
        assert "attachments" in body
        assert "text" in body

    def test_dispatch_discord_posts_embed(self, mock_async):
        d = NotificationDispatcher(channels=[_discord_channel()])
        results = d.dispatch_sync(_make_alert())
        assert results[0].success is True
        body = json.loads(mock_async.requests[0].content)
        assert "embeds" in body

    def test_dispatch_telegram_calls_send_message_endpoint(self, mock_async):
        d = NotificationDispatcher(channels=[_telegram_channel()])
        results = d.dispatch_sync(_make_alert())
        assert results[0].success is True
        req = mock_async.requests[0]
        assert req.url.host == "api.telegram.org"
        assert req.url.path.startswith("/bot")
        assert req.url.path.endswith("/sendMessage")
        # chat_id should be a query string param.
        assert "chat_id=-100123" in str(req.url)
        body = json.loads(req.content)
        assert body["parse_mode"] == "Markdown"
        assert "GitHub" in body["text"]

    def test_dispatch_telegram_surfaces_ok_false_body(self, mock_async):
        mock_async.status_code = 200
        mock_async.json_response = {"ok": False, "description": "chat not found"}
        d = NotificationDispatcher(channels=[_telegram_channel()])
        results = d.dispatch_sync(_make_alert())
        assert results[0].success is False
        assert "chat not found" in (results[0].error or "")

    def test_dispatch_webhook_uses_legacy_shape(self, mock_async):
        d = NotificationDispatcher(channels=[_webhook_channel()])
        results = d.dispatch_sync(_make_alert())
        assert results[0].success is True
        body = json.loads(mock_async.requests[0].content)
        assert body["service_name"] == "GitHub"
        assert body["type"] == "down"
        assert "timestamp" in body

    def test_dispatch_fans_out_to_multiple_channels(self, mock_async):
        d = NotificationDispatcher(
            channels=[_slack_channel(), _discord_channel(), _webhook_channel()]
        )
        results = d.dispatch_sync(_make_alert())
        assert len(results) == 3
        assert all(r.success for r in results)
        assert len(mock_async.requests) == 3
        hosts = {r.url.host for r in mock_async.requests}
        assert "hooks.slack.com" in hosts
        assert "discord.com" in hosts
        assert "example.com" in hosts

    def test_dispatch_5xx_is_failure(self, mock_async):
        mock_async.status_code = 500
        d = NotificationDispatcher(channels=[_slack_channel()])
        results = d.dispatch_sync(_make_alert())
        assert results[0].success is False
        assert results[0].status_code == 500
        assert results[0].error  # captured response text

    def test_dispatch_respects_service_alert_channels_override(self, mock_async):
        d = NotificationDispatcher(
            channels=[_slack_channel("a"), _discord_channel("b")]
        )
        svc = _service(alert_channels=["b"])
        results = d.dispatch_sync(_make_alert(), svc)
        assert len(results) == 1
        assert results[0].channel.name == "b"

    def test_channel_send_result_to_dict_shape(self):
        ch = _slack_channel()
        r = ChannelSendResult(channel=ch, success=True, status_code=200)
        d = r.to_dict()
        assert d == {
            "channel": "slack-ops",
            "type": "slack",
            "success": True,
            "status_code": 200,
            "error": None,
        }
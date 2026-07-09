"""Notification channel dispatchers.

Each supported backend (Slack, Discord, Telegram, generic webhook) renders
an :class:`~pulseboard.alerting.Alert` into the payload shape that backend
expects, then sends it via ``httpx``. The :class:`NotificationDispatcher`
coordinates sending to multiple channels in parallel.

The design is intentionally narrow: the dispatcher is constructed from a
list of :class:`~pulseboard.models.NotificationChannel` objects (built
once at config-load time) and exposes a single ``dispatch`` entry point.
Failures in one channel never prevent the others from firing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from .alerting import Alert, AlertType
from .models import (
    ChannelType,
    NotificationChannel,
    STATUS_COLOR,
    STATUS_EMOJI,
)

if TYPE_CHECKING:
    from .models import ServiceConfig


# Cap individual outbound requests at 10s -- long enough to tolerate a slow
# corporate proxy, short enough that a hung Slack webhook won't block the
# watcher loop for the rest of its check interval.
HTTP_TIMEOUT_SECONDS = 10.0


class ChannelSendResult:
    """Outcome of sending an alert to a single channel.

    Returned as a list from :meth:`NotificationDispatcher.dispatch` so
    callers (and the ``notify-test`` command) can show per-channel
    success/failure with the HTTP status code that came back.
    """

    __slots__ = ("channel", "success", "status_code", "error")

    def __init__(
        self,
        channel: NotificationChannel,
        success: bool,
        status_code: int | None = None,
        error: str | None = None,
    ) -> None:
        self.channel = channel
        self.success = success
        self.status_code = status_code
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel.name,
            "type": self.channel.channel_type.value,
            "success": self.success,
            "status_code": self.status_code,
            "error": self.error,
        }

    def __str__(self) -> str:  # pragma: no cover - debug only
        if self.success:
            return f"{self.channel.name}: OK ({self.status_code})"
        return f"{self.channel.name}: FAIL ({self.status_code}) {self.error}"


# ---------------------------------------------------------------------------
# Payload renderers -- pure functions, no I/O. Easy to unit-test.
# ---------------------------------------------------------------------------


def _alert_title(alert: Alert) -> str:
    """Short headline for the rendered notification."""
    emoji = STATUS_EMOJI.get(alert.result.status, "\u26aa")
    if alert.alert_type == AlertType.DOWN:
        return f"{emoji} {alert.service_name} is DOWN"
    if alert.alert_type == AlertType.RECOVERY:
        return f"{emoji} {alert.service_name} recovered"
    if alert.alert_type == AlertType.DEGRADED:
        return f"{emoji} {alert.service_name} is DEGRADED"
    return f"{emoji} {alert.service_name} status changed"


def _alert_description(alert: Alert) -> str:
    """Two-to-three-line body used by every backend."""
    lines = [alert.message]
    latency = alert.result.latency_ms
    if latency:
        lines.append(f"Latency: {latency:.0f} ms")
    if alert.result.error and alert.alert_type != AlertType.DOWN:
        lines.append(f"Reason: {alert.result.error}")
    if alert.result.status_code is not None:
        lines.append(f"Status code: {alert.result.status_code}")
    lines.append(
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    return "\n".join(lines)


def render_slack_payload(alert: Alert) -> dict[str, Any]:
    """Render an Alert as a Slack incoming-webhook payload.

    Slack incoming-webhooks accept both Block Kit (``blocks``) and the
    classic ``attachments`` array. We send attachments because they're
    the most widely-supported form (including legacy custom integrations)
    and let us color the sidebar via the ``color`` hex field.
    """
    title = _alert_title(alert)
    description = _alert_description(alert)
    color_hex = f"#{STATUS_COLOR.get(alert.result.status, 0x95A5A6):06X}"
    return {
        "text": title,  # fallback for clients that don't render attachments
        "attachments": [
            {
                "color": color_hex,
                "title": title,
                "text": description,
                "footer": "PulseBoard",
                "ts": int(alert.timestamp.timestamp()),
            }
        ],
    }


def render_discord_payload(alert: Alert) -> dict[str, Any]:
    """Render an Alert as a Discord webhook embed payload.

    Discord webhooks accept a top-level ``content`` (plain mention text)
    plus an ``embeds`` array. Each embed can carry a single 24-bit color
    in its ``color`` field, which is exactly what we use to encode the
    status.
    """
    title = _alert_title(alert)
    description = _alert_description(alert)
    fields: list[dict[str, Any]] = []
    if alert.result.status_code is not None:
        fields.append(
            {
                "name": "Status code",
                "value": str(alert.result.status_code),
                "inline": True,
            }
        )
    if alert.result.latency_ms:
        fields.append(
            {
                "name": "Latency",
                "value": f"{alert.result.latency_ms:.0f} ms",
                "inline": True,
            }
        )
    fields.append({"name": "Alert", "value": alert.alert_type, "inline": True})
    return {
        "content": title,
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": STATUS_COLOR.get(alert.result.status, 0x95A5A6),
                "fields": fields,
                "footer": {"text": "PulseBoard"},
                "timestamp": alert.timestamp.isoformat(),
            }
        ],
    }


def render_telegram_payload(alert: Alert) -> dict[str, Any]:
    """Render an Alert as a Telegram Bot API ``sendMessage`` payload.

    Telegram's ``sendMessage`` endpoint takes the chat_id, the text, and
    an optional parse_mode. We use Markdown for headings and code, which
    is supported by every modern Telegram client.
    """
    title = _alert_title(alert)
    description = _alert_description(alert)
    text = f"*{title}*\n\n{description}\n\n_PulseBoard alert_"
    return {
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }


def render_webhook_payload(alert: Alert) -> dict[str, Any]:
    """Render an Alert as the original generic JSON payload.

    Preserves the on-the-wire shape that ``alert_webhook`` users already
    depend on, so adding the dispatcher is fully backwards-compatible.
    """
    return alert.to_dict()


# ---------------------------------------------------------------------------
# Per-channel senders -- thin async wrappers around the renderers.
# ---------------------------------------------------------------------------


async def _post_json(
    channel: NotificationChannel, url: str, payload: dict[str, Any]
) -> ChannelSendResult:
    """POST a JSON payload to ``url`` and convert the response into a result."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload)
        ok = 200 <= resp.status_code < 300
        return ChannelSendResult(
            channel=channel,
            success=ok,
            status_code=resp.status_code,
            error=None if ok else resp.text[:200],
        )
    except Exception as e:
        return ChannelSendResult(
            channel=channel,
            success=False,
            status_code=None,
            error=f"{type(e).__name__}: {e}",
        )


async def _send_slack(
    channel: NotificationChannel, alert: Alert
) -> ChannelSendResult:
    assert channel.webhook_url is not None  # validated upstream
    return await _post_json(channel, channel.webhook_url, render_slack_payload(alert))


async def _send_discord(
    channel: NotificationChannel, alert: Alert
) -> ChannelSendResult:
    assert channel.webhook_url is not None
    return await _post_json(channel, channel.webhook_url, render_discord_payload(alert))


async def _send_telegram(
    channel: NotificationChannel, alert: Alert
) -> ChannelSendResult:
    assert channel.telegram_token is not None
    assert channel.telegram_chat_id is not None
    payload = render_telegram_payload(alert)
    url = f"https://api.telegram.org/bot{channel.telegram_token}/sendMessage"
    # Telegram wants chat_id as a query string param alongside the JSON body.
    # httpx's ``params`` keeps the body JSON-clean.
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                url,
                params={"chat_id": channel.telegram_chat_id},
                json=payload,
            )
        ok = 200 <= resp.status_code < 300
        # Telegram returns 200 with {"ok": false} on chat errors -- surface
        # the description if present so failures are debuggable.
        err: str | None = None
        if not ok:
            err = resp.text[:200]
        else:
            try:
                body = resp.json()
                if isinstance(body, dict) and body.get("ok") is False:
                    err = body.get("description", "telegram ok=false")
                    ok = False
            except Exception:
                pass
        return ChannelSendResult(
            channel=channel, success=ok, status_code=resp.status_code, error=err
        )
    except Exception as e:
        return ChannelSendResult(
            channel=channel,
            success=False,
            status_code=None,
            error=f"{type(e).__name__}: {e}",
        )


async def _send_webhook(
    channel: NotificationChannel, alert: Alert
) -> ChannelSendResult:
    assert channel.webhook_url is not None
    return await _post_json(channel, channel.webhook_url, render_webhook_payload(alert))


_SENDERS = {
    ChannelType.SLACK: _send_slack,
    ChannelType.DISCORD: _send_discord,
    ChannelType.TELEGRAM: _send_telegram,
    ChannelType.WEBHOOK: _send_webhook,
}


# ---------------------------------------------------------------------------
# Dispatcher -- fans an alert out to N channels in parallel.
# ---------------------------------------------------------------------------


class NotificationDispatcher:
    """Send an Alert to one or more channels.

    Channels are passed in at construction time and validated up-front so
    that dispatching is a no-IO-validation call. The dispatcher resolves
    per-service channel overrides (a service that lists
    ``alert_channels: ["ops"]`` only fires to ``ops``; services with no
    override fire to every configured channel).
    """

    def __init__(self, channels: list[NotificationChannel] | None = None) -> None:
        self.channels: list[NotificationChannel] = list(channels or [])
        self._by_name: dict[str, NotificationChannel] = {
            c.name: c for c in self.channels
        }

    @classmethod
    def from_config(
        cls, raw_channels: list[dict[str, Any]] | None
    ) -> "NotificationDispatcher":
        """Build a dispatcher from the YAML ``notification_channels`` block.

        ``ValueError`` from a missing/invalid field propagates so config
        loading fails loudly instead of silently dropping alerts.
        """
        channels: list[NotificationChannel] = []
        for entry in raw_channels or []:
            try:
                ch = NotificationChannel(
                    name=entry["name"],
                    channel_type=ChannelType(entry.get("type", "webhook").lower()),
                    webhook_url=entry.get("webhook_url"),
                    telegram_token=entry.get("telegram_token"),
                    telegram_chat_id=(
                        str(entry["telegram_chat_id"])
                        if entry.get("telegram_chat_id") is not None
                        else None
                    ),
                    options=entry.get("options", {}) or {},
                )
            except KeyError as e:
                raise ValueError(
                    f"notification_channels entry missing required field {e}: {entry}"
                ) from e
            ch.validate()
            channels.append(ch)
        return cls(channels)

    def resolve_channels(
        self, service: "ServiceConfig | None"
    ) -> list[NotificationChannel]:
        """Pick which channels fire for a given service.

        - If the service lists ``alert_channels``, only those (that exist)
          fire.
        - If it doesn't, all configured channels fire.
        - Service-level ``alert_webhook`` (the legacy field) continues to
          work -- we synthesize an ad-hoc webhook channel for it.
        """
        channels: list[NotificationChannel] = []
        if service is not None and service.alert_channels:
            for name in service.alert_channels:
                if name not in self._by_name:
                    # Config bug -- silently skip rather than crash the
                    # watcher loop, but make it visible in the result.
                    continue
                channels.append(self._by_name[name])
        else:
            channels = list(self.channels)

        # Legacy alert_webhook support: if a service has the old field
        # set and didn't already route to a webhook channel, synthesize
        # one on the fly so existing configs keep working.
        if service is not None and service.alert_webhook:
            already_has_webhook = any(
                c.channel_type == ChannelType.WEBHOOK
                and c.webhook_url == service.alert_webhook
                for c in channels
            )
            if not already_has_webhook:
                channels.append(
                    NotificationChannel(
                        name=f"{service.name}:alert_webhook",
                        channel_type=ChannelType.WEBHOOK,
                        webhook_url=service.alert_webhook,
                    )
                )
        return channels

    async def dispatch(
        self, alert: Alert, service: "ServiceConfig | None" = None
    ) -> list[ChannelSendResult]:
        """Fan an alert out to every applicable channel in parallel."""
        targets = self.resolve_channels(service)
        if not targets:
            return []
        results = await asyncio.gather(
            *(_SENDERS[ch.channel_type](ch, alert) for ch in targets),
            return_exceptions=False,
        )
        return list(results)

    def dispatch_sync(
        self, alert: Alert, service: "ServiceConfig | None" = None
    ) -> list[ChannelSendResult]:
        """Convenience wrapper for callers that aren't already in an event loop."""
        return asyncio.run(self.dispatch(alert, service))

"""Alerting system — webhooks, terminal bell, and structured alerts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx

from .models import CheckResult, Status


class AlertManager:
    """Manages alert state and dispatches notifications."""

    def __init__(self, alert_on_recovery: bool = True):
        self.alert_on_recovery = alert_on_recovery
        self._previous_status: dict[str, Status] = {}

    def evaluate(self, result: CheckResult) -> Alert | None:
        """Evaluate a check result and return an Alert if state changed."""
        prev = self._previous_status.get(result.service_name)
        self._previous_status[result.service_name] = result.status

        if prev is None:
            # First check — only alert if down
            if result.status == Status.DOWN:
                return Alert(
                    service_name=result.service_name,
                    alert_type=AlertType.DOWN,
                    result=result,
                    message=f"🔴 {result.service_name} is DOWN: {result.error}",
                )
            return None

        # Status change detection
        if prev != result.status:
            if result.status == Status.DOWN:
                return Alert(
                    service_name=result.service_name,
                    alert_type=AlertType.DOWN,
                    result=result,
                    message=f"🔴 {result.service_name} went DOWN: {result.error}",
                )
            elif result.status == Status.UP and prev == Status.DOWN:
                if self.alert_on_recovery:
                    return Alert(
                        service_name=result.service_name,
                        alert_type=AlertType.RECOVERY,
                        result=result,
                        message=f"🟢 {result.service_name} recovered ({result.latency_ms:.0f}ms)",
                    )
            elif result.status == Status.DEGRADED:
                return Alert(
                    service_name=result.service_name,
                    alert_type=AlertType.DEGRADED,
                    result=result,
                    message=f"🟡 {result.service_name} degraded: {result.error}",
                )
        return None

    def reset(self) -> None:
        """Clear all tracked state."""
        self._previous_status.clear()


class AlertType(str):
    DOWN = "down"
    RECOVERY = "recovery"
    DEGRADED = "degraded"


class Alert:
    """A single alert event."""

    def __init__(
        self,
        service_name: str,
        alert_type: str,
        result: CheckResult,
        message: str,
    ):
        self.service_name = service_name
        self.alert_type = alert_type
        self.result = result
        self.message = message
        self.timestamp = datetime.now(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_name": self.service_name,
            "type": self.alert_type,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "latency_ms": self.result.latency_ms,
            "error": self.result.error,
        }

    def __str__(self) -> str:
        return self.message


async def send_webhook_alert(webhook_url: str, alert: Alert) -> bool:
    """Send an alert to a webhook URL. Returns True on success."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                webhook_url,
                json=alert.to_dict(),
                headers={"Content-Type": "application/json"},
            )
            return resp.status_code < 400
    except Exception:
        return False


def terminal_alert(alert: Alert) -> None:
    """Print an alert to the terminal with a bell character."""
    print(f"\a{alert.message}")

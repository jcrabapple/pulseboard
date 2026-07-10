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
        # Per-service count of consecutive non-UP checks. Incremented on
        # every DOWN or DEGRADED result, reset to 0 when the service is UP.
        self._consecutive_failures: dict[str, int] = {}

    def evaluate(self, result: CheckResult) -> Alert | None:
        """Evaluate a check result and return an Alert if state changed."""
        prev = self._previous_status.get(result.service_name)
        self._previous_status[result.service_name] = result.status

        # Update the consecutive-failure counter: any non-UP status
        # increments; a UP status resets to 0.
        if result.status == Status.UP:
            self._consecutive_failures[result.service_name] = 0
        else:
            self._consecutive_failures[result.service_name] = (
                self._consecutive_failures.get(result.service_name, 0) + 1
            )
        failures = self._consecutive_failures[result.service_name]

        if prev is None:
            # First check — only alert if down
            if result.status == Status.DOWN:
                return Alert(
                    service_name=result.service_name,
                    alert_type=AlertType.DOWN,
                    result=result,
                    message=f"🔴 {result.service_name} is DOWN: {result.error}",
                    consecutive_failures=failures,
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
                    consecutive_failures=failures,
                )
            elif result.status == Status.UP and prev == Status.DOWN:
                if self.alert_on_recovery:
                    return Alert(
                        service_name=result.service_name,
                        alert_type=AlertType.RECOVERY,
                        result=result,
                        message=f"🟢 {result.service_name} recovered ({result.latency_ms:.0f}ms)",
                        consecutive_failures=failures,
                    )
            elif result.status == Status.DEGRADED:
                return Alert(
                    service_name=result.service_name,
                    alert_type=AlertType.DEGRADED,
                    result=result,
                    message=f"🟡 {result.service_name} degraded: {result.error}",
                    consecutive_failures=failures,
                )
        return None

    def previous_status(self, service_name: str) -> Status | None:
        """Return the last status observed for a service (or None).

        Exposed so callers can capture the prior status *before*
        :meth:`evaluate` mutates internal state — used by the incident
        recorder to know the ``from_status`` of a transition.
        """
        return self._previous_status.get(service_name)

    def reset(self) -> None:
        """Clear all tracked state."""
        self._previous_status.clear()
        self._consecutive_failures.clear()


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
        consecutive_failures: int = 0,
    ) -> None:
        self.service_name = service_name
        self.alert_type = alert_type
        self.result = result
        self.message = message
        self.timestamp = datetime.now(timezone.utc)
        self.consecutive_failures = consecutive_failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_name": self.service_name,
            "type": self.alert_type,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "latency_ms": self.result.latency_ms,
            "error": self.result.error,
            "consecutive_failures": self.consecutive_failures,
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

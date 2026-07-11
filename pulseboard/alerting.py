"""Alerting system — webhooks, terminal bell, and structured alerts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict

import httpx

from .models import CheckResult, Status


class AlertManager:
    """Manages alert state and dispatches notifications."""

    def __init__(
        self,
        alert_on_recovery: bool = True,
        alert_cooldown_seconds: float = 0.0,
        re_alert_every_n_failures: int = 0,
        clock: Callable[[], datetime] | None = None,
    ):
        self.alert_on_recovery = alert_on_recovery
        self.alert_cooldown_seconds = alert_cooldown_seconds
        # When > 0, refire an alert of the current alert type every Nth
        # consecutive failure so a missed initial alert doesn't leave
        # the user blind during a long outage. 0 disables (the default)
        # — existing behaviour unchanged.
        self.re_alert_every_n_failures = re_alert_every_n_failures
        self._clock = clock
        self._previous_status: dict[str, Status] = {}
        # Per-service count of consecutive non-UP checks. Incremented on
        # every DOWN or DEGRADED result, reset to 0 when the service is UP.
        self._consecutive_failures: dict[str, int] = {}
        # Per (service_name, alert_type) timestamp of the last alert fired.
        # Used to suppress duplicate alerts within the cooldown window.
        self._last_alert_at: Dict[tuple[str, str], datetime] = {}

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        return datetime.now(timezone.utc)

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
                return self._maybe_suppress(
                    result.service_name,
                    AlertType.DOWN,
                    Alert(
                        service_name=result.service_name,
                        alert_type=AlertType.DOWN,
                        result=result,
                        message=f"🔴 {result.service_name} is DOWN: {result.error}",
                        consecutive_failures=failures,
                    ),
                )
            return None

        # Status change detection
        if prev != result.status:
            if result.status == Status.DOWN:
                return self._maybe_suppress(
                    result.service_name,
                    AlertType.DOWN,
                    Alert(
                        service_name=result.service_name,
                        alert_type=AlertType.DOWN,
                        result=result,
                        message=f"🔴 {result.service_name} went DOWN: {result.error}",
                        consecutive_failures=failures,
                    ),
                )
            elif result.status == Status.UP and prev == Status.DOWN:
                if self.alert_on_recovery:
                    return self._maybe_suppress(
                        result.service_name,
                        AlertType.RECOVERY,
                        Alert(
                            service_name=result.service_name,
                            alert_type=AlertType.RECOVERY,
                            result=result,
                            message=f"🟢 {result.service_name} recovered ({result.latency_ms:.0f}ms)",
                            consecutive_failures=failures,
                        ),
                    )
            elif result.status == Status.DEGRADED:
                return self._maybe_suppress(
                    result.service_name,
                    AlertType.DEGRADED,
                    Alert(
                        service_name=result.service_name,
                        alert_type=AlertType.DEGRADED,
                        result=result,
                        message=f"🟡 {result.service_name} degraded: {result.error}",
                        consecutive_failures=failures,
                    ),
                )
        return self._maybe_re_alert(result, failures)

    def _maybe_re_alert(
        self, result: CheckResult, failures: int
    ) -> Alert | None:
        """Refire an alert when the threshold is configured and the
        current alert type is non-UP and ``failures`` is a positive
        multiple of the threshold.

        Uses ``_maybe_suppress`` so cooldown dedup still applies to
        re-alerts within the same window.
        """
        if self.re_alert_every_n_failures <= 0:
            return None
        if result.status == Status.UP:
            return None
        if failures <= 0 or failures % self.re_alert_every_n_failures != 0:
            return None

        # service didn't actually transition, use previous status as
        # ``prev`` was already updated in evaluate.
        prev_status = self._previous_status.get(result.service_name)
        alert_type = AlertType.DOWN if prev_status == Status.DOWN else AlertType.DEGRADED
        msg_prefix = "🔴" if alert_type == AlertType.DOWN else "🟡"
        return self._maybe_suppress(
            result.service_name,
            alert_type,
            Alert(
                service_name=result.service_name,
                alert_type=alert_type,
                result=result,
                message=f"{msg_prefix} {result.service_name} still DOWN after {failures} failures: {result.error}",
                consecutive_failures=failures,
            ),
        )

    def _maybe_suppress(
        self, service_name: str, alert_type: str, alert: Alert
    ) -> Alert | None:
        """Return the alert unless we already fired an alert of the same
        type for this service within the cooldown window.

        Recoveries are always passed through: a recovery indicates
        resolution and the user should see it even if a prior DOWN alert
        is still inside cooldown.
        """
        now = self._now()
        if alert_type == AlertType.RECOVERY:
            self._last_alert_at[(service_name, alert_type)] = now
            return alert

        if self.alert_cooldown_seconds > 0:
            key = (service_name, alert_type)
            last = self._last_alert_at.get(key)
            if last is not None and (now - last).total_seconds() < self.alert_cooldown_seconds:
                return None

        self._last_alert_at[(service_name, alert_type)] = now
        return alert

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
        self._last_alert_at.clear()


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
            "status": self.result.status.value,
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

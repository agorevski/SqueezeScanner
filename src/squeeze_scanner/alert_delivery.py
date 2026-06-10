from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertDeliveryMessage:
    """Structured notification payload for an alert event."""

    payload: dict[str, Any]


@dataclass(frozen=True)
class AlertDeliveryResult:
    status: str
    response: Mapping[str, Any] | None = None
    error_message: str | None = None

    @classmethod
    def success(cls, response: Mapping[str, Any] | None = None) -> "AlertDeliveryResult":
        return cls(status="success", response=response or {})

    @classmethod
    def failure(cls, error_message: str, response: Mapping[str, Any] | None = None) -> "AlertDeliveryResult":
        return cls(status="failure", response=response or {}, error_message=error_message)


@dataclass(frozen=True)
class AlertDeliveryOutcome:
    channel: str
    destination: str
    status: str
    response: Mapping[str, Any]
    error_message: str | None = None


class AlertDeliveryChannel(Protocol):
    name: str
    destination: str

    def send(self, message: AlertDeliveryMessage) -> AlertDeliveryResult:
        ...


class NoopAlertDeliveryChannel:
    name = "noop"
    destination = "dry-run"

    def send(self, message: AlertDeliveryMessage) -> AlertDeliveryResult:
        logger.info("Dry-run alert delivery: %s", message.payload.get("text", "alert triggered"))
        return AlertDeliveryResult.success({"dry_run": True})


class WebhookAlertDeliveryChannel:
    name = "webhook"

    def __init__(self, url: str, timeout_seconds: float = 5.0) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.destination = _safe_destination_label(url)

    def send(self, message: AlertDeliveryMessage) -> AlertDeliveryResult:
        body = json.dumps(message.payload, sort_keys=True).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "squeeze-scanner-alerts/0.1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                status_code = int(getattr(response, "status", response.getcode()))
        except urllib.error.HTTPError as exc:
            return AlertDeliveryResult.failure(
                f"Webhook returned HTTP {exc.code}.",
                {"status_code": exc.code},
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None) or str(exc)
            return AlertDeliveryResult.failure(f"Webhook request failed: {reason}")

        if 200 <= status_code < 300:
            return AlertDeliveryResult.success({"status_code": status_code})
        return AlertDeliveryResult.failure(f"Webhook returned HTTP {status_code}.", {"status_code": status_code})


class AlertDeliveryService:
    def __init__(
        self,
        channels: Sequence[AlertDeliveryChannel] = (),
        *,
        default_channels: Sequence[str] = (),
        public_base_url: str | None = None,
    ) -> None:
        self.channels = {channel.name: channel for channel in channels}
        self.default_channels = normalize_delivery_channels(default_channels)
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None

    def deliver(
        self,
        alert: Mapping[str, Any],
        event: Mapping[str, Any],
        channel_names: Sequence[str],
    ) -> list[AlertDeliveryOutcome]:
        message = AlertDeliveryMessage(build_alert_delivery_payload(alert, event, self.public_base_url))
        outcomes: list[AlertDeliveryOutcome] = []
        for channel_name in normalize_delivery_channels(channel_names):
            channel = self.channels.get(channel_name)
            if channel is None:
                outcomes.append(
                    AlertDeliveryOutcome(
                        channel=channel_name,
                        destination="unconfigured",
                        status="failure",
                        response={},
                        error_message=f"Alert delivery channel {channel_name!r} is not configured.",
                    )
                )
                continue

            try:
                result = channel.send(message)
            except Exception as exc:
                logger.exception("Alert delivery channel %s raised an exception.", channel_name)
                result = AlertDeliveryResult.failure(str(exc))

            status = "success" if result.status == "success" else "failure"
            outcomes.append(
                AlertDeliveryOutcome(
                    channel=channel_name,
                    destination=channel.destination,
                    status=status,
                    response=dict(result.response or {}),
                    error_message=result.error_message,
                )
            )
        return outcomes


def build_alert_delivery_service(
    *,
    default_channels: Sequence[str] = (),
    webhook_url: str | None = None,
    webhook_timeout_seconds: float = 5.0,
    public_base_url: str | None = None,
) -> AlertDeliveryService:
    channels: list[AlertDeliveryChannel] = [NoopAlertDeliveryChannel()]
    if webhook_url:
        channels.append(WebhookAlertDeliveryChannel(webhook_url, timeout_seconds=webhook_timeout_seconds))
    return AlertDeliveryService(channels, default_channels=default_channels, public_base_url=public_base_url)


def build_alert_delivery_payload(
    alert: Mapping[str, Any],
    event: Mapping[str, Any],
    public_base_url: str | None = None,
) -> dict[str, Any]:
    result = event.get("result")
    result = result if isinstance(result, Mapping) else {}
    symbol = str(event.get("symbol") or "").upper()
    link = f"{public_base_url}/?symbol={symbol}&alert_event_id={event.get('id')}" if public_base_url and symbol else None
    risk_flags = result.get("risk_flags")
    if risk_flags is None:
        risk_flags = result.get("warnings", [])
    payload = {
        "text": event.get("message"),
        "symbol": symbol,
        "score": result.get("score"),
        "model_confidence": result.get("model_confidence"),
        "risk_flags": risk_flags,
        "link": link,
        "alert": {
            "id": alert.get("id"),
            "name": alert.get("name"),
            "rule": alert.get("rule"),
        },
        "trigger": {
            "event_id": event.get("id"),
            "rule_type": event.get("rule_type"),
            "condition_key": event.get("condition_key"),
            "message": event.get("message"),
            "value": event.get("value"),
            "threshold": event.get("threshold"),
            "previous_value": event.get("previous_value"),
            "created_at": event.get("created_at"),
        },
    }
    return payload


def normalize_delivery_channels(channels: Sequence[str] | None) -> list[str]:
    normalized: list[str] = []
    for channel in channels or ():
        value = str(channel).strip().lower().replace("-", "_")
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def _safe_destination_label(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "configured webhook"

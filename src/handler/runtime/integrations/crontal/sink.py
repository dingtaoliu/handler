"""Crontal notification sink: push `notification.created` to Crontal's bell.

POST <crontal origin>/api/v1/handler/callbacks/notifications with the body
signed like Crontal's own webhooks to `/ingest/crontal`, in reverse:
`X-Handler-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>." + body)>`
using this instance's HANDLER_WEBHOOK_SECRET, plus `X-Handler-Instance: <name>`
so Crontal can pick the registered instance whose secret to check. Crontal
rejects timestamps outside a 5-minute window, so each attempt is re-signed.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from ...sinks import NOTIFICATION_CREATED, DeliveryError, DomainEvent, RetryPolicy
from ...web.ingest import sign

CALLBACK_PATH = "/api/v1/handler/callbacks/notifications"
SIGNATURE_HEADER = "X-Handler-Signature"
INSTANCE_HEADER = "X-Handler-Instance"
WIRE_FIELDS = ("id", "instance", "recipients", "message", "thread_key", "run_id",
               "thread_type", "thread_title", "created_at")
# Bad requests never succeed on retry; everything else (401 during secret
# rotation, 404 before Crontal ships the endpoint, 5xx, timeouts) may.
PERMANENT_STATUS = {400, 413, 422}


class CrontalNotificationSink:
    name = "crontal.notifications"

    def __init__(self, crontal_origin: str, webhook_secret: str, instance: str, *,
                 retry: RetryPolicy | None = None, timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = crontal_origin.rstrip("/") + CALLBACK_PATH
        self.secret = webhook_secret
        self.instance = instance or "main"
        self.retry = retry or RetryPolicy()
        self.timeout = timeout
        self.transport = transport

    def accepts(self, event: DomainEvent) -> bool:
        return event.type == NOTIFICATION_CREATED

    def body(self, payload: dict[str, Any]) -> bytes:
        data = payload["data"]
        wire = {name: data.get(name) for name in WIRE_FIELDS}
        wire["instance"] = self.instance
        return json.dumps(wire, separators=(",", ":"), ensure_ascii=False).encode()

    async def deliver(self, payload: dict[str, Any]) -> None:
        body = self.body(payload)
        headers = {"Content-Type": "application/json",
                   SIGNATURE_HEADER: sign(self.secret, body),
                   INSTANCE_HEADER: self.instance}
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            response = await client.post(self.url, content=body, headers=headers)
        if 200 <= response.status_code < 300:
            return
        raise DeliveryError(f"HTTP {response.status_code}: {response.text[:300]}",
                            retryable=response.status_code not in PERMANENT_STATUS)


def crontal_sink_from_config(crontal_url: str | None, agent_token: str | None,
                             webhook_secret: str | None, instance: str):
    """The sink when this instance is Crontal-connected (URL, agent token and
    webhook secret all configured), else None."""
    if not (crontal_url and agent_token and webhook_secret):
        return None
    from ...gateway.crontal_api import crontal_base_url
    return CrontalNotificationSink(crontal_base_url(crontal_url), webhook_secret, instance)

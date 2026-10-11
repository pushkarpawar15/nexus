from __future__ import annotations

import contextvars
import json
import logging
import re
import time
import uuid
from collections.abc import Iterable
from typing import Any

_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "nexus_correlation_id", default="-"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)([\"']?\b(?:authorization|token|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|api[_-]?key|github[_-]?token|password|secret|private[_-]?key)"
    r"\b[\"']?\s*[:=]\s*)([\"']?)(?:bearer\s+)?([^\"'\s,;}]+)([\"']?)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def current_correlation_id() -> str:
    return _correlation_id.get()


def redact_credentials(value: str) -> str:
    value = _SENSITIVE_ASSIGNMENT.sub(r"\1\2[REDACTED]\4", value)
    return _BEARER.sub("Bearer [REDACTED]", value)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = redact_credentials(record.getMessage())
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "severity": record.levelname,
            "logger": record.name,
            "message": message,
            "correlation_id": getattr(record, "correlation_id", current_correlation_id()),
        }
        if record.exc_info:
            payload["exception"] = redact_credentials(self.formatException(record.exc_info))
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


class CorrelationIdMiddleware:
    """Pure ASGI middleware that accepts safe request IDs and returns them to callers."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_id = next(
            (
                value.decode("latin-1")
                for name, value in scope.get("headers", [])
                if name.lower() == b"x-correlation-id"
                and _CORRELATION_ID.fullmatch(value.decode("latin-1"))
            ),
            str(uuid.uuid4()),
        )
        token = _correlation_id.set(request_id)

        async def send_with_correlation(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-correlation-id"
                ]
                headers.append((b"x-correlation-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_correlation)
        finally:
            _correlation_id.reset(token)


def configure_logging(
    level: str = "INFO", *, handlers: Iterable[logging.Handler] | None = None
) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())
    output_handlers = list(handlers) if handlers is not None else [logging.StreamHandler()]
    for handler in output_handlers:
        handler.setFormatter(JsonLogFormatter())
        root.addHandler(handler)


__all__ = [
    "CorrelationIdMiddleware",
    "JsonLogFormatter",
    "configure_logging",
    "current_correlation_id",
    "redact_credentials",
]

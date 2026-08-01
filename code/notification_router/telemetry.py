"""Redacted call logs and immutable per-attempt accounting."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from io import TextIOBase
from pathlib import Path
from typing import Mapping


_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|cookie|password|secret|token)"
    r"(\s*[:=]\s*|\s+)[^\s,;]+"
)
_LONG_TOKEN_PATTERN = re.compile(r"\b(?:[A-Za-z0-9_-]{32,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_.-]+)\b")
MAX_DETAIL_CHARS = 256


def redact_text(value: object) -> str:
    """Redact credential-shaped material and bound diagnostic text."""

    text = str(value)
    text = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _LONG_TOKEN_PATTERN.sub("[REDACTED]", text)
    if len(text) > MAX_DETAIL_CHARS:
        text = text[:MAX_DETAIL_CHARS] + "…"
    return text


def safe_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class AttemptAccounting:
    attempt: int
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_usd: float
    success: bool
    error_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "success": self.success,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class CallAccounting:
    call_id: str
    stage: str
    operation: str
    provider: str
    model: str
    attempts: tuple[AttemptAccounting, ...]

    @property
    def latency_ms(self) -> float:
        return sum(attempt.latency_ms for attempt in self.attempts)

    @property
    def input_tokens(self) -> int | None:
        values = [attempt.input_tokens for attempt in self.attempts if attempt.input_tokens is not None]
        return sum(values) if values else None

    @property
    def output_tokens(self) -> int | None:
        values = [attempt.output_tokens for attempt in self.attempts if attempt.output_tokens is not None]
        return sum(values) if values else None

    @property
    def total_tokens(self) -> int | None:
        values = [attempt.total_tokens for attempt in self.attempts if attempt.total_tokens is not None]
        return sum(values) if values else None

    @property
    def cost_usd(self) -> float:
        return sum(attempt.cost_usd for attempt in self.attempts)

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    def as_dict(self) -> dict[str, object]:
        return {
            "call_id": self.call_id,
            "stage": self.stage,
            "operation": self.operation,
            "provider": self.provider,
            "model": self.model,
            "attempt_count": self.attempt_count,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "attempts": [attempt.as_dict() for attempt in self.attempts],
        }


class RedactedCallLogger:
    """Append-only JSONL logger that never accepts raw request content."""

    def __init__(
        self,
        *,
        stream: TextIOBase | None = None,
        path: str | Path | None = None,
    ) -> None:
        if stream is not None and path is not None:
            raise ValueError("choose stream or path, not both")
        self._stream = stream
        self._path = Path(path).resolve() if path is not None else None
        self._lock = threading.Lock()

    def _emit(self, event: str, fields: Mapping[str, object]) -> None:
        record = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            **{key: value for key, value in fields.items()},
        }
        serialized = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock:
            if self._stream is not None:
                self._stream.write(serialized + "\n")
                self._stream.flush()
            elif self._path is not None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(serialized + "\n")
            else:
                # The default is deliberately quiet; callers may opt into a file or stream.
                return

    def request_started(
        self,
        *,
        call_id: str,
        stage: str,
        operation: str,
        provider: str,
        model: str,
        message_id: str | None,
        media_id: str | None,
        payload_bytes: int,
    ) -> None:
        self._emit(
            "provider_request_started",
            {
                "call_id": call_id,
                "stage": stage,
                "operation": operation,
                "provider": provider,
                "model": model,
                "message_hash": safe_identifier(message_id),
                "media_hash": safe_identifier(media_id),
                "payload_bytes": payload_bytes,
            },
        )

    def request_finished(self, *, accounting: CallAccounting) -> None:
        self._emit(
            "provider_request_finished",
            {
                "call_id": accounting.call_id,
                "stage": accounting.stage,
                "operation": accounting.operation,
                "provider": accounting.provider,
                "model": accounting.model,
                "accounting": accounting.as_dict(),
            },
        )

    def request_error(
        self,
        *,
        call_id: str,
        stage: str,
        operation: str,
        provider: str,
        model: str,
        attempt: int,
        code: str,
        detail: object,
    ) -> None:
        self._emit(
            "provider_request_error",
            {
                "call_id": call_id,
                "stage": stage,
                "operation": operation,
                "provider": provider,
                "model": model,
                "attempt": attempt,
                "error_code": code,
                "detail": redact_text(detail),
            },
        )


NULL_LOGGER = RedactedCallLogger(stream=None)

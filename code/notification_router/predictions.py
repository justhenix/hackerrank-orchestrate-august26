"""Raw, model-independent prediction records and contract checks."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from .schemas import ACTION_VALUES, MESSAGE_TYPE_VALUES


@dataclass(frozen=True, slots=True)
class RawPrediction:
    """A preserved routing proposal; no deterministic confidence finalization."""

    message_id: str
    action: str | None
    message_type: str | None
    reason: str | None
    selected_evidence_message_ids: tuple[str, ...]
    confidence: float | None
    latency_ms: float | None = None
    cost_usd: float | None = None
    raw_response: Any = None
    error_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "action": self.action,
            "message_type": self.message_type,
            "reason": self.reason,
            "selected_evidence_message_ids": list(self.selected_evidence_message_ids),
            "confidence": self.confidence,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "raw_response": self.raw_response,
            "error_code": self.error_code,
        }


def prediction_from_mapping(row: Mapping[str, object]) -> RawPrediction:
    """Convert an external raw proposal without repairing or scoring it."""

    selected = row.get("selected_evidence_message_ids", ())
    if isinstance(selected, str) or selected is None:
        raise ValueError("selected evidence must be a sequence of IDs")
    return RawPrediction(
        message_id=row.get("message_id"),
        action=row.get("action"),
        message_type=row.get("message_type"),
        reason=row.get("reason"),
        selected_evidence_message_ids=tuple(selected),
        confidence=row.get("confidence"),
        latency_ms=row.get("latency_ms"),
        cost_usd=row.get("cost_usd"),
        raw_response=row.get("raw_response"),
        error_code=row.get("error_code"),
    )


def prediction_schema_errors(prediction: RawPrediction) -> tuple[str, ...]:
    """Return stable field-level errors without changing the raw proposal."""

    errors: list[str] = []
    if not isinstance(prediction.message_id, str) or not prediction.message_id:
        errors.append("message_id")
    if prediction.action not in ACTION_VALUES:
        errors.append("action")
    if prediction.message_type not in MESSAGE_TYPE_VALUES:
        errors.append("message_type")
    if not isinstance(prediction.reason, str) or not prediction.reason.strip():
        errors.append("reason")
    if any(
        not isinstance(evidence_id, str) or not evidence_id
        for evidence_id in prediction.selected_evidence_message_ids
    ):
        errors.append("selected_evidence_message_ids")
    if len(set(prediction.selected_evidence_message_ids)) != len(
        prediction.selected_evidence_message_ids
    ):
        errors.append("duplicate_selected_evidence_message_ids")
    if isinstance(prediction.confidence, bool) or not isinstance(
        prediction.confidence, (int, float)
    ):
        errors.append("confidence")
    elif not math.isfinite(float(prediction.confidence)) or not 0 <= float(
        prediction.confidence
    ) <= 1:
        errors.append("confidence")
    for field_name, value in (
        ("latency_ms", prediction.latency_ms),
        ("cost_usd", prediction.cost_usd),
    ):
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            errors.append(field_name)
    return tuple(errors)

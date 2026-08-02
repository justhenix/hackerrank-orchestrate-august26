"""Strict model-facing contracts for extraction and raw routing proposals."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Mapping, Sequence

from .schemas import ACTION_VALUES, MESSAGE_TYPE_VALUES


MEDIA_STATE_VALUES = frozenset(
    {
        "not_applicable",
        "ok",
        "missing",
        "unsupported",
        "decode_failed",
        "empty_extraction",
        "low_quality",
    }
)
EXTRACTION_STATE_VALUES = MEDIA_STATE_VALUES - {"not_applicable"}
MEDIA_FORMAT_VALUES = frozenset(
    {"jpeg", "png", "webp", "avif", "mp3", "m4a", "wav", "unknown"}
)
EXTRACTION_RECORD_KEYS = (
    "media_id",
    "content_sha256",
    "declared_path",
    "detected_format",
    "media_state",
    "extractor_name",
    "extractor_version",
    "extractor_config_sha256",
    "extraction_schema_version",
    "extracted_text",
    "factual_description",
    "language",
    "quality_score",
    "quality_reasons",
    "created_at",
)
ROUTING_RESPONSE_KEYS = (
    "action",
    "message_type",
    "reason",
    "selected_evidence_message_ids",
    "routing_uncertainty",
    "uncertainty_reasons",
    "semantic_flags",
    "deadline_at",
    "semantic_support",
    "reported_contradictory_signal_count",
)
SEMANTIC_FLAG_KEYS = (
    "time_critical",
    "indirectly_addresses_user",
    "transactional",
    "promotional",
    "credential_or_secret_request",
    "impersonation_or_domain_concern",
    "suspicious_link_or_payment_request",
    "warning_or_quoted_discussion",
)
SEMANTIC_SUPPORT_KEYS = (
    "flag",
    "source_field",
    "start_char",
    "end_char_exclusive",
)
SEMANTIC_SOURCE_FIELDS = frozenset(
    {"message_text", "extracted_text", "factual_description"}
)
MAX_EVIDENCE_IDS = 3
MAX_REASON_CHARS = 200
MAX_REASON_WORDS = 24
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def extraction_response_schema() -> dict[str, object]:
    """Return the provider-facing JSON Schema for ``ExtractionRecord``."""

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "media_id": {"type": "string"},
            "content_sha256": {
                "anyOf": [
                    {"type": "string", "pattern": _SHA256_RE.pattern[1:-1]},
                    {"type": "null"},
                ]
            },
            "declared_path": {"type": "string"},
            "detected_format": {"type": "string", "enum": sorted(MEDIA_FORMAT_VALUES)},
            "media_state": {"type": "string", "enum": sorted(EXTRACTION_STATE_VALUES)},
            "extractor_name": {"type": "string"},
            "extractor_version": {"type": "string"},
            "extractor_config_sha256": {
                "type": "string",
                "pattern": _SHA256_RE.pattern[1:-1],
            },
            "extraction_schema_version": {"type": "string"},
            "extracted_text": {"type": "string"},
            "factual_description": {"type": "string"},
            "language": {"type": "string"},
            "quality_score": {"type": "number", "minimum": 0, "maximum": 1},
            "quality_reasons": {"type": "array", "items": {"type": "string"}},
            "created_at": {"type": "string"},
        },
        "required": list(EXTRACTION_RECORD_KEYS),
    }


def routing_response_schema() -> dict[str, object]:
    """Return the provider-facing JSON Schema for ``RawRoutingDecision``."""

    semantic_support_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "flag": {"type": "string", "enum": sorted(SEMANTIC_FLAG_KEYS)},
            "source_field": {
                "type": "string",
                "enum": sorted(SEMANTIC_SOURCE_FIELDS),
            },
            "start_char": {"type": "integer", "minimum": 0},
            "end_char_exclusive": {"type": "integer", "minimum": 0},
        },
        "required": list(SEMANTIC_SUPPORT_KEYS),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": sorted(ACTION_VALUES)},
            "message_type": {"type": "string", "enum": sorted(MESSAGE_TYPE_VALUES)},
            "reason": {"type": "string", "maxLength": MAX_REASON_CHARS},
            "selected_evidence_message_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_EVIDENCE_IDS,
            },
            "routing_uncertainty": {"type": "number", "minimum": 0, "maximum": 1},
            "uncertainty_reasons": {"type": "array", "items": {"type": "string"}},
            "semantic_flags": {
                "type": "object",
                "additionalProperties": False,
                "properties": {flag: {"type": "boolean"} for flag in SEMANTIC_FLAG_KEYS},
                "required": list(SEMANTIC_FLAG_KEYS),
            },
            "deadline_at": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
            },
            "semantic_support": {
                "type": "array",
                "items": semantic_support_item,
                "maxItems": len(SEMANTIC_FLAG_KEYS),
            },
            "reported_contradictory_signal_count": {
                "type": "integer",
                "minimum": 0,
            },
        },
        "required": list(ROUTING_RESPONSE_KEYS),
    }


class StructuredOutputError(ValueError):
    """Raised when provider output violates a frozen structured contract."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "SCHEMA_INVALID",
        field: str | None = None,
        constraint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.constraint = constraint

    def as_machine_readable(self) -> dict[str, str]:
        """Return bounded retry feedback without model response content."""

        feedback = {"code": self.code}
        if self.field is not None:
            feedback["field"] = self.field
        if self.constraint is not None:
            feedback["constraint"] = self.constraint
        return feedback


def _reject_constant(value: str) -> object:
    raise StructuredOutputError(
        f"non-finite JSON constant {value!r} is not allowed", code="JSON_INVALID"
    )


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StructuredOutputError(
                f"duplicate JSON key {key!r}", code="JSON_DUPLICATE_KEY"
            )
        result[key] = value
    return result


def _strict_json_object(raw: bytes | str, *, name: str) -> Mapping[str, object]:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StructuredOutputError(
                f"{name} is not UTF-8", code="JSON_INVALID"
            ) from exc
    elif isinstance(raw, str):
        text = raw
    else:
        raise StructuredOutputError(f"{name} must be JSON text", code="JSON_INVALID")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except StructuredOutputError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StructuredOutputError(f"{name} is not valid JSON", code="JSON_INVALID") from exc
    if not isinstance(value, Mapping):
        raise StructuredOutputError(f"{name} must be a JSON object", code="SCHEMA_INVALID")
    return value


def _exact_keys(
    value: Mapping[str, object], expected: Sequence[str], *, name: str
) -> None:
    actual = set(value)
    expected_set = set(expected)
    missing = sorted(expected_set - actual)
    extra = sorted(actual - expected_set)
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("extra=" + ",".join(extra))
        raise StructuredOutputError(
            f"{name} keys invalid: {'; '.join(detail)}", code="SCHEMA_INVALID"
        )


def _string(value: object, *, name: str, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise StructuredOutputError(f"{name} must be a string", code="SCHEMA_INVALID")
    return value


def _number(value: object, *, name: str, minimum: float = 0.0, maximum: float = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StructuredOutputError(f"{name} must be a number", code="SCHEMA_INVALID")
    number = float(value)
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise StructuredOutputError(f"{name} is outside its allowed range", code="SCHEMA_INVALID")
    return number


def _nonnegative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StructuredOutputError(f"{name} must be a non-negative integer", code="SCHEMA_INVALID")
    return value


def _string_list(value: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise StructuredOutputError(f"{name} must be an array", code="SCHEMA_INVALID")
    result = tuple(_string(item, name=f"{name}[]") for item in value)
    if len(set(result)) != len(result):
        raise StructuredOutputError(f"{name} contains duplicates", code="SCHEMA_INVALID")
    return result


@dataclass(frozen=True, slots=True)
class ExtractionRecord:
    """Contract-valid, untrusted media extraction output."""

    media_id: str
    content_sha256: str | None
    declared_path: str
    detected_format: str
    media_state: str
    extractor_name: str
    extractor_version: str
    extractor_config_sha256: str
    extraction_schema_version: str
    extracted_text: str
    factual_description: str
    language: str
    quality_score: float
    quality_reasons: tuple[str, ...]
    created_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "media_id": self.media_id,
            "content_sha256": self.content_sha256,
            "declared_path": self.declared_path,
            "detected_format": self.detected_format,
            "media_state": self.media_state,
            "extractor_name": self.extractor_name,
            "extractor_version": self.extractor_version,
            "extractor_config_sha256": self.extractor_config_sha256,
            "extraction_schema_version": self.extraction_schema_version,
            "extracted_text": self.extracted_text,
            "factual_description": self.factual_description,
            "language": self.language,
            "quality_score": self.quality_score,
            "quality_reasons": list(self.quality_reasons),
            "created_at": self.created_at.isoformat(),
        }


def parse_extraction_record(raw: bytes | str) -> ExtractionRecord:
    value = _strict_json_object(raw, name="extraction response")
    _exact_keys(value, EXTRACTION_RECORD_KEYS, name="extraction response")
    media_id = _string(value["media_id"], name="media_id")
    content_hash = value["content_sha256"]
    if content_hash is not None and (
        not isinstance(content_hash, str) or not _SHA256_RE.fullmatch(content_hash)
    ):
        raise StructuredOutputError("content_sha256 is not a lowercase SHA-256", code="SCHEMA_INVALID")
    declared_path = _string(value["declared_path"], name="declared_path")
    detected_format = _string(value["detected_format"], name="detected_format")
    if detected_format not in MEDIA_FORMAT_VALUES:
        raise StructuredOutputError("detected_format is unsupported", code="SCHEMA_INVALID")
    media_state = _string(value["media_state"], name="media_state")
    if media_state not in EXTRACTION_STATE_VALUES:
        raise StructuredOutputError("media_state is invalid", code="SCHEMA_INVALID")
    if content_hash is None and media_state != "missing":
        raise StructuredOutputError("only missing media may omit content_sha256", code="SCHEMA_INVALID")
    extractor_name = _string(value["extractor_name"], name="extractor_name")
    extractor_version = _string(value["extractor_version"], name="extractor_version")
    extractor_config_hash = _string(
        value["extractor_config_sha256"], name="extractor_config_sha256"
    )
    if not _SHA256_RE.fullmatch(extractor_config_hash):
        raise StructuredOutputError(
            "extractor_config_sha256 is not a lowercase SHA-256", code="SCHEMA_INVALID"
        )
    extraction_schema_version = _string(
        value["extraction_schema_version"], name="extraction_schema_version"
    )
    extracted_text = _string(value["extracted_text"], name="extracted_text", nonempty=False)
    factual_description = _string(
        value["factual_description"], name="factual_description", nonempty=False
    )
    if media_state in {"missing", "unsupported", "decode_failed", "empty_extraction"} and (
        extracted_text or factual_description
    ):
        raise StructuredOutputError(
            f"{media_state} media cannot contain semantic extraction", code="SCHEMA_INVALID"
        )
    language = _string(value["language"], name="language")
    quality_score = _number(value["quality_score"], name="quality_score")
    quality_reasons = _string_list(value["quality_reasons"], name="quality_reasons")
    created_at_value = _string(value["created_at"], name="created_at")
    try:
        created_at = datetime.fromisoformat(created_at_value)
    except ValueError as exc:
        raise StructuredOutputError("created_at is not ISO-8601", code="SCHEMA_INVALID") from exc
    return ExtractionRecord(
        media_id=media_id,
        content_sha256=content_hash,
        declared_path=declared_path,
        detected_format=detected_format,
        media_state=media_state,
        extractor_name=extractor_name,
        extractor_version=extractor_version,
        extractor_config_sha256=extractor_config_hash,
        extraction_schema_version=extraction_schema_version,
        extracted_text=extracted_text,
        factual_description=factual_description,
        language=language,
        quality_score=quality_score,
        quality_reasons=quality_reasons,
        created_at=created_at,
    )


def validate_extraction_record(
    record: ExtractionRecord,
    *,
    media_id: str,
    declared_path: str,
    detected_format: str,
    content_sha256: str | None,
    created_at: datetime,
) -> None:
    """Bind provider output to the request that produced it."""

    if record.media_id != media_id:
        raise StructuredOutputError("extraction media_id does not match request", code="SCHEMA_INVALID")
    if record.declared_path != declared_path:
        raise StructuredOutputError("extraction path does not match request", code="SCHEMA_INVALID")
    if record.detected_format != detected_format:
        raise StructuredOutputError(
            "extraction detected_format does not match request", code="SCHEMA_INVALID"
        )
    if record.content_sha256 != content_sha256:
        raise StructuredOutputError(
            "extraction content hash does not match request", code="SCHEMA_INVALID"
        )
    if record.created_at != created_at:
        raise StructuredOutputError("extraction timestamp does not match request", code="SCHEMA_INVALID")


@dataclass(frozen=True, slots=True)
class SemanticSupport:
    flag: str
    source_field: str
    start_char: int
    end_char_exclusive: int

    def as_dict(self) -> dict[str, object]:
        return {
            "flag": self.flag,
            "source_field": self.source_field,
            "start_char": self.start_char,
            "end_char_exclusive": self.end_char_exclusive,
        }


@dataclass(frozen=True, slots=True)
class RawRoutingDecision:
    """Raw model proposal; deliberately has no final confidence field."""

    action: str
    message_type: str
    reason: str
    selected_evidence_message_ids: tuple[str, ...]
    routing_uncertainty: float
    uncertainty_reasons: tuple[str, ...]
    semantic_flags: Mapping[str, bool]
    deadline_at: datetime | None
    semantic_support: tuple[SemanticSupport, ...]
    reported_contradictory_signal_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "message_type": self.message_type,
            "reason": self.reason,
            "selected_evidence_message_ids": list(self.selected_evidence_message_ids),
            "routing_uncertainty": self.routing_uncertainty,
            "uncertainty_reasons": list(self.uncertainty_reasons),
            "semantic_flags": dict(self.semantic_flags),
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
            "semantic_support": [support.as_dict() for support in self.semantic_support],
            "reported_contradictory_signal_count": self.reported_contradictory_signal_count,
        }


def parse_routing_decision(
    raw: bytes | str,
    *,
    allowed_evidence_message_ids: Sequence[str] = (),
) -> RawRoutingDecision:
    value = _strict_json_object(raw, name="routing response")
    _exact_keys(value, ROUTING_RESPONSE_KEYS, name="routing response")
    action = _string(value["action"], name="action")
    if action not in ACTION_VALUES:
        raise StructuredOutputError("action is outside the decision contract", code="SCHEMA_INVALID")
    message_type = _string(value["message_type"], name="message_type")
    if message_type not in MESSAGE_TYPE_VALUES:
        raise StructuredOutputError(
            "message_type is outside the decision contract", code="SCHEMA_INVALID"
        )
    reason = _string(value["reason"], name="reason")
    if not reason.strip() or len(reason) > MAX_REASON_CHARS or len(reason.split()) > MAX_REASON_WORDS:
        raise StructuredOutputError("reason exceeds the decision contract bounds", code="SCHEMA_INVALID")
    selected = _string_list(
        value["selected_evidence_message_ids"], name="selected_evidence_message_ids"
    )
    if len(selected) > MAX_EVIDENCE_IDS:
        raise StructuredOutputError("selected evidence exceeds the configured limit", code="SCHEMA_INVALID")
    allowed = tuple(allowed_evidence_message_ids)
    if any(message_id not in allowed for message_id in selected):
        raise StructuredOutputError("selected evidence is outside the allowlist", code="EVIDENCE_NOT_ALLOWED")
    positions = {message_id: index for index, message_id in enumerate(allowed)}
    if tuple(sorted(selected, key=positions.__getitem__)) != selected:
        raise StructuredOutputError("selected evidence order is not canonical", code="EVIDENCE_NOT_ALLOWED")
    routing_uncertainty = _number(
        value["routing_uncertainty"], name="routing_uncertainty"
    )
    uncertainty_reasons = _string_list(
        value["uncertainty_reasons"], name="uncertainty_reasons"
    )
    flags_value = value["semantic_flags"]
    if not isinstance(flags_value, Mapping):
        raise StructuredOutputError("semantic_flags must be an object", code="SCHEMA_INVALID")
    _exact_keys(flags_value, SEMANTIC_FLAG_KEYS, name="semantic_flags")
    flags: dict[str, bool] = {}
    for flag in SEMANTIC_FLAG_KEYS:
        if not isinstance(flags_value[flag], bool):
            raise StructuredOutputError(f"semantic_flags.{flag} must be boolean", code="SCHEMA_INVALID")
        flags[flag] = flags_value[flag]
    deadline_value = value["deadline_at"]
    deadline_at: datetime | None
    if deadline_value is None:
        deadline_at = None
    else:
        deadline_text = _string(deadline_value, name="deadline_at")
        try:
            deadline_at = datetime.fromisoformat(deadline_text)
        except ValueError as exc:
            raise StructuredOutputError("deadline_at is not ISO-8601", code="SCHEMA_INVALID") from exc
    support_value = value["semantic_support"]
    if not isinstance(support_value, list):
        raise StructuredOutputError("semantic_support must be an array", code="SCHEMA_INVALID")
    supports: list[SemanticSupport] = []
    seen_flags: set[str] = set()
    for index, item in enumerate(support_value):
        if not isinstance(item, Mapping):
            raise StructuredOutputError(
                f"semantic_support[{index}] must be an object", code="SCHEMA_INVALID"
            )
        _exact_keys(item, SEMANTIC_SUPPORT_KEYS, name=f"semantic_support[{index}]")
        flag = _string(item["flag"], name=f"semantic_support[{index}].flag")
        if flag not in SEMANTIC_FLAG_KEYS or flag in seen_flags:
            raise StructuredOutputError(
                f"semantic_support[{index}].flag is invalid or duplicated",
                code="SCHEMA_INVALID",
                field=f"semantic_support[{index}].flag",
                constraint="unique_flag_support",
            )
        if not flags[flag]:
            raise StructuredOutputError(
                f"semantic_support[{index}] supports a false flag", code="SCHEMA_INVALID"
            )
        source_field = _string(
            item["source_field"], name=f"semantic_support[{index}].source_field"
        )
        if source_field not in SEMANTIC_SOURCE_FIELDS:
            raise StructuredOutputError(
                f"semantic_support[{index}].source_field is invalid", code="SCHEMA_INVALID"
            )
        start_char = _nonnegative_int(
            item["start_char"], name=f"semantic_support[{index}].start_char"
        )
        end_char = _nonnegative_int(
            item["end_char_exclusive"],
            name=f"semantic_support[{index}].end_char_exclusive",
        )
        if end_char <= start_char:
            raise StructuredOutputError(
                f"semantic_support[{index}] span is empty", code="SCHEMA_INVALID"
            )
        seen_flags.add(flag)
        supports.append(
            SemanticSupport(
                flag=flag,
                source_field=source_field,
                start_char=start_char,
                end_char_exclusive=end_char,
            )
        )
    missing_support = [flag for flag, enabled in flags.items() if enabled and flag not in seen_flags]
    if missing_support:
        raise StructuredOutputError(
            "true semantic flags require support spans", code="SCHEMA_INVALID"
        )
    if not flags["time_critical"] and deadline_at is not None:
        raise StructuredOutputError(
            "deadline_at requires time_critical", code="SCHEMA_INVALID"
        )
    contradiction_count = _nonnegative_int(
        value["reported_contradictory_signal_count"],
        name="reported_contradictory_signal_count",
    )
    return RawRoutingDecision(
        action=action,
        message_type=message_type,
        reason=reason,
        selected_evidence_message_ids=selected,
        routing_uncertainty=routing_uncertainty,
        uncertainty_reasons=uncertainty_reasons,
        semantic_flags=MappingProxyType(flags),
        deadline_at=deadline_at,
        semantic_support=tuple(supports),
        reported_contradictory_signal_count=contradiction_count,
    )


def validate_routing_decision_against_packet(
    decision: RawRoutingDecision, packet: Mapping[str, object]
) -> None:
    """Validate model-reported support spans against packet data."""

    allowlist_value = packet.get("allowed_evidence_message_ids")
    if not isinstance(allowlist_value, (list, tuple)):
        raise StructuredOutputError("packet evidence allowlist is invalid", code="PACKET_SCHEMA_INVALID")
    allowlist = tuple(allowlist_value)
    if len(set(allowlist)) != len(allowlist) or any(
        selected not in allowlist for selected in decision.selected_evidence_message_ids
    ):
        raise StructuredOutputError("selected evidence is outside the packet allowlist", code="EVIDENCE_NOT_ALLOWED")
    positions = {value: index for index, value in enumerate(allowlist)}
    if tuple(sorted(decision.selected_evidence_message_ids, key=positions.__getitem__)) != tuple(
        decision.selected_evidence_message_ids
    ):
        raise StructuredOutputError("selected evidence is not packet-ordered", code="EVIDENCE_NOT_ALLOWED")
    message = packet.get("message")
    media = packet.get("media")
    if not isinstance(message, Mapping) or not isinstance(media, Mapping):
        raise StructuredOutputError("packet shape is invalid", code="PACKET_SCHEMA_INVALID")
    record = media.get("record")
    record_mapping = record if isinstance(record, Mapping) else {}
    source_values: dict[str, str] = {
        "message_text": message.get("message_text", "")
        if isinstance(message.get("message_text", ""), str)
        else "",
        "extracted_text": record_mapping.get("extracted_text", "")
        if isinstance(record_mapping.get("extracted_text", ""), str)
        else "",
        "factual_description": record_mapping.get("factual_description", "")
        if isinstance(record_mapping.get("factual_description", ""), str)
        else "",
    }
    for support in decision.semantic_support:
        source = source_values[support.source_field]
        if support.end_char_exclusive > len(source):
            raise StructuredOutputError(
                "semantic support span exceeds packet field", code="SCHEMA_INVALID"
            )
        if not source[support.start_char : support.end_char_exclusive]:
            raise StructuredOutputError("semantic support span is empty", code="SCHEMA_INVALID")

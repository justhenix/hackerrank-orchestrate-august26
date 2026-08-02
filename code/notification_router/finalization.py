"""Strict S8 safety validation and deterministic final decision creation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import MappingProxyType
from typing import Mapping

from .confidence import ConfidenceAudit, calculate_final_confidence, compute_contradiction_count
from .contracts import ExtractionRecord, RawRoutingDecision, StructuredOutputError
from .features import DeterministicFeatures
from .inputs import SanitizedMessage
from .packet import RoutingPacket
from .predictions import RawPrediction
from .retrieval import EvidenceProvenanceError, RetrievalResult, validate_selected_evidence


@dataclass(frozen=True, slots=True)
class SafetyAudit:
    """Deterministically recomputed S8 invariant state."""

    invariant_version: str
    high_risk_required_mute: bool
    promotion_optout_required_mute: bool
    near_term: bool
    contradiction_count: int
    triggered_invariants: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "invariant_version": self.invariant_version,
            "high_risk_required_mute": self.high_risk_required_mute,
            "promotion_optout_required_mute": self.promotion_optout_required_mute,
            "near_term": self.near_term,
            "contradiction_count": self.contradiction_count,
            "triggered_invariants": list(self.triggered_invariants),
        }


SAFETY_INVARIANTS_VERSION = "safety-invariants-v0"
FALLBACK_REASON = "Routing unavailable; queued safely for later review."


def _high_risk_required_mute(
    decision: RawRoutingDecision,
    features: DeterministicFeatures,
) -> bool:
    flags = decision.semantic_flags
    request_signal = bool(
        flags.get("credential_or_secret_request")
        or (
            flags.get("suspicious_link_or_payment_request")
            and flags.get("impersonation_or_domain_concern")
        )
    )
    identity_signal = bool(
        (features.domain_mismatch and features.young_sender_domain)
        or features.high_business_reports
        or features.prior_same_sender_report
        or (features.domain_mismatch and not features.trusted_business)
    )
    path_a = identity_signal and request_signal
    path_b = (
        not features.prior_same_sender_interaction
        and flags.get("credential_or_secret_request")
        and flags.get("suspicious_link_or_payment_request")
    )
    negative_control = bool(
        (
            features.trusted_business
            and flags.get("transactional")
            and not flags.get("credential_or_secret_request")
        )
        or (
            (features.trusted_group_sender or features.trusted_personal_sender)
            and flags.get("transactional")
            and not flags.get("credential_or_secret_request")
            and not flags.get("suspicious_link_or_payment_request")
        )
        or (
            flags.get("warning_or_quoted_discussion")
            and not flags.get("indirectly_addresses_user")
            and not features.explicit_user_id_mention
        )
    )
    return bool((path_a or path_b) and not negative_control)


def _promotion_optout_required_mute(
    message: SanitizedMessage,
    decision: RawRoutingDecision,
    features: DeterministicFeatures,
) -> bool:
    return bool(
        features.business_relationship_present
        and features.promotion_opt_out_at is not None
        and features.promotion_opt_out_at < message.created_at
        and decision.semantic_flags.get("promotional")
        and not decision.semantic_flags.get("transactional")
    )


def _near_term(message: SanitizedMessage, decision: RawRoutingDecision) -> bool:
    if not decision.semantic_flags.get("time_critical") or decision.deadline_at is None:
        return False
    return bool(
        message.created_at < decision.deadline_at
        <= message.created_at + timedelta(hours=2)
    )


def _validate_reason(
    decision: RawRoutingDecision,
    packet: RoutingPacket,
) -> None:
    reason = decision.reason.lower()
    allowlist = packet.as_dict().get("allowed_evidence_message_ids", ())
    if any(isinstance(message_id, str) and message_id.lower() in reason for message_id in allowlist):
        raise StructuredOutputError(
            "reason must not contain evidence IDs",
            code="REASON_INVALID",
            field="reason",
            constraint="evidence_id_in_reason",
        )
    injection_phrases = (
        "ignore previous instructions",
        "ignore all instructions",
        "system message",
        "developer message",
    )
    if any(phrase in reason for phrase in injection_phrases):
        raise StructuredOutputError(
            "reason contains an instruction-like phrase",
            code="REASON_INVALID",
            field="reason",
            constraint="instruction_like_reason",
        )


def validate_routing_safety(
    *,
    message: SanitizedMessage,
    packet: RoutingPacket,
    decision: RawRoutingDecision,
    features: DeterministicFeatures,
    retrieval: RetrievalResult,
) -> SafetyAudit:
    """Validate action/evidence safety without mutating the model decision."""

    if decision.deadline_at is not None and (
        decision.deadline_at.tzinfo is not None
        and decision.deadline_at.utcoffset() is not None
    ):
        raise StructuredOutputError(
            "deadline_at must use dataset-local naive time",
            code="SCHEMA_INVALID",
            field="deadline_at",
            constraint="naive_dataset_timestamp",
        )
    _validate_reason(decision, packet)
    try:
        validate_selected_evidence(
            decision.selected_evidence_message_ids,
            retrieval.allowed_evidence_message_ids,
        )
    except EvidenceProvenanceError as exc:
        raise StructuredOutputError(
            str(exc),
            code="EVIDENCE_NOT_ALLOWED",
            field="selected_evidence_message_ids",
            constraint="allowlist_provenance",
        ) from exc

    packet_constraints = packet.as_dict().get("safety_constraints", {})
    allowed_actions = tuple(packet_constraints.get("allowed_actions", ()))
    if decision.action not in allowed_actions:
        raise StructuredOutputError(
            "action is outside packet safety constraints",
            code="ACTION_CONSTRAINT_VIOLATION",
            field="action",
            constraint="allowed_actions",
        )
    packet_required = packet_constraints.get("required_action")
    if packet_required is not None and decision.action != packet_required:
        raise StructuredOutputError(
            "action does not satisfy packet required_action",
            code="ACTION_CONSTRAINT_VIOLATION",
            field="action",
            constraint="required_action",
        )

    high_risk = _high_risk_required_mute(decision, features)
    promotion_optout = _promotion_optout_required_mute(message, decision, features)
    near_term = _near_term(message, decision)
    quiet_exception = near_term and bool(
        features.trusted_business
        or features.trusted_group_sender
        or features.trusted_personal_sender
        or features.explicit_user_id_mention
    ) and not high_risk
    group_exception = bool(
        (
            decision.semantic_flags.get("time_critical")
            and features.explicit_user_id_mention
        )
        or (features.trusted_group_sender and near_term)
    ) and not high_risk
    prohibited = set(packet_constraints.get("prohibited_actions", ()))
    if decision.action == "notify" and "notify" in prohibited:
        if features.quiet_hours and not quiet_exception:
            raise StructuredOutputError(
                "notify is prohibited during quiet hours",
                code="ACTION_CONSTRAINT_VIOLATION",
                field="action",
                constraint="INV-103",
            )
        if features.group_muted and not group_exception:
            raise StructuredOutputError(
                "notify is prohibited for a muted group",
                code="ACTION_CONSTRAINT_VIOLATION",
                field="action",
                constraint="INV-104",
            )
    if high_risk:
        if decision.action != "mute":
            raise StructuredOutputError(
                "corroborated high-risk request requires mute",
                code="ACTION_CONSTRAINT_VIOLATION",
                field="action",
                constraint="INV-101",
            )
        if decision.message_type not in {"scam", "spam", "unknown"}:
            raise StructuredOutputError(
                "high-risk mute has an incompatible message type",
                code="ACTION_CONSTRAINT_VIOLATION",
                field="message_type",
                constraint="INV-101_type",
            )
    if promotion_optout:
        if decision.action != "mute":
            raise StructuredOutputError(
                "promotion opt-out requires mute",
                code="ACTION_CONSTRAINT_VIOLATION",
                field="action",
                constraint="INV-102",
            )
        if decision.message_type not in {"promotion", "spam"}:
            raise StructuredOutputError(
                "promotion opt-out has an incompatible message type",
                code="ACTION_CONSTRAINT_VIOLATION",
                field="message_type",
                constraint="INV-102_type",
            )
    contradictions = compute_contradiction_count(decision, message)
    triggered = list(packet_constraints.get("triggered_invariants", ()))
    if high_risk:
        triggered.append("INV-101")
    if promotion_optout:
        triggered.append("INV-102")
    if contradictions >= 3:
        raise StructuredOutputError(
            "contradictory hard signals require degraded routing",
            code="SEMANTIC_FLAG_CONTRADICTION",
            field="semantic_flags",
            constraint="contradiction_limit",
        )
    return SafetyAudit(
        invariant_version=SAFETY_INVARIANTS_VERSION,
        high_risk_required_mute=high_risk,
        promotion_optout_required_mute=promotion_optout,
        near_term=near_term,
        contradiction_count=contradictions,
        triggered_invariants=tuple(dict.fromkeys(triggered)),
    )


@dataclass(frozen=True, slots=True)
class FinalDecision:
    """Contract-valid final decision plus out-of-band deterministic audits."""

    message_id: str
    action: str
    message_type: str
    reason: str
    confidence: float
    selected_evidence_message_ids: tuple[str, ...]
    raw_decision: Mapping[str, object]
    safety_audit: Mapping[str, object]
    confidence_audit: Mapping[str, object]
    degraded: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "action": self.action,
            "message_type": self.message_type,
            "reason": self.reason,
            "confidence": self.confidence,
            "selected_evidence_message_ids": list(self.selected_evidence_message_ids),
            "raw_decision": dict(self.raw_decision),
            "safety_audit": dict(self.safety_audit),
            "confidence_audit": dict(self.confidence_audit),
            "degraded": self.degraded,
        }

    def as_prediction(self, *, error_code: str | None = None) -> RawPrediction:
        return RawPrediction(
            message_id=self.message_id,
            action=self.action,
            message_type=self.message_type,
            reason=self.reason,
            selected_evidence_message_ids=self.selected_evidence_message_ids,
            confidence=self.confidence,
            error_code=error_code,
        )


def finalize_routing_decision(
    *,
    message: SanitizedMessage,
    packet: RoutingPacket,
    decision: RawRoutingDecision,
    features: DeterministicFeatures,
    extraction_record: ExtractionRecord | None,
    retrieval: RetrievalResult,
    routing_attempt_count: int,
) -> FinalDecision:
    safety = validate_routing_safety(
        message=message,
        packet=packet,
        decision=decision,
        features=features,
        retrieval=retrieval,
    )
    confidence = calculate_final_confidence(
        message=message,
        packet=packet,
        decision=decision,
        features=features,
        extraction_record=extraction_record,
        retrieval=retrieval,
        routing_attempt_count=routing_attempt_count,
        contradiction_count=safety.contradiction_count,
    )
    return FinalDecision(
        message_id=message.message_id,
        action=decision.action,
        message_type=decision.message_type,
        reason=decision.reason,
        confidence=confidence.rounded_confidence,
        selected_evidence_message_ids=decision.selected_evidence_message_ids,
        raw_decision=MappingProxyType(decision.as_dict()),
        safety_audit=MappingProxyType(safety.as_dict()),
        confidence_audit=MappingProxyType(confidence.as_dict()),
        degraded=False,
    )


def degraded_final_decision(
    *,
    message: SanitizedMessage,
    packet: RoutingPacket,
    features: DeterministicFeatures,
    extraction_record: ExtractionRecord | None,
    retrieval: RetrievalResult,
    error_code: str,
) -> FinalDecision:
    """Emit the declared generic degraded decision without repairing output."""

    flags = MappingProxyType(
        {
            "time_critical": False,
            "indirectly_addresses_user": False,
            "transactional": False,
            "promotional": False,
            "credential_or_secret_request": False,
            "impersonation_or_domain_concern": False,
            "suspicious_link_or_payment_request": False,
            "warning_or_quoted_discussion": False,
        }
    )
    raw = RawRoutingDecision(
        action="digest",
        message_type="unknown",
        reason=FALLBACK_REASON,
        selected_evidence_message_ids=(),
        routing_uncertainty=1.0,
        uncertainty_reasons=(error_code,),
        semantic_flags=flags,
        deadline_at=None,
        semantic_support=(),
        reported_contradictory_signal_count=0,
    )
    confidence = calculate_final_confidence(
        message=message,
        packet=packet,
        decision=raw,
        features=features,
        extraction_record=extraction_record,
        retrieval=retrieval,
        routing_attempt_count=0,
        degraded=True,
    )
    safety = SafetyAudit(
        invariant_version=SAFETY_INVARIANTS_VERSION,
        high_risk_required_mute=False,
        promotion_optout_required_mute=False,
        near_term=False,
        contradiction_count=0,
        triggered_invariants=tuple(packet.as_dict().get("safety_constraints", {}).get("triggered_invariants", ())),
    )
    return FinalDecision(
        message_id=message.message_id,
        action=raw.action,
        message_type=raw.message_type,
        reason=raw.reason,
        confidence=confidence.rounded_confidence,
        selected_evidence_message_ids=(),
        raw_decision=MappingProxyType(raw.as_dict()),
        safety_audit=MappingProxyType(safety.as_dict()),
        confidence_audit=MappingProxyType(confidence.as_dict()),
        degraded=True,
    )

"""Deterministic PROVISIONAL-V0 confidence policy for S8 finalization."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Sequence

from .contracts import ExtractionRecord, RawRoutingDecision
from .features import DeterministicFeatures
from .inputs import SanitizedMessage
from .packet import RoutingPacket
from .retrieval import RetrievalResult


CONFIDENCE_POLICY_VERSION = "confidence-policy-provisional-v0"
CONFIDENCE_FLOOR = 0.05
CONFIDENCE_CEILING = 0.98
PENALTY_CAP = 0.35
PERSONALIZATION_TERMS = frozenset(
    {
        "history",
        "historical",
        "prior",
        "previous",
        "trusted",
        "trust",
        "replied",
        "opened",
        "dismissed",
        "reported",
        "preference",
        "repeated",
        "repetition",
        "relationship",
        "engagement",
    }
)
CONTENT_INTRINSIC_TYPES = frozenset({"greeting", "forward", "spam", "scam"})


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


def _usable_text(value: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", value.strip()))


def _reason_claims_personalization(reason: str) -> bool:
    tokens = set(re.findall(r"[a-z0-9]+", reason.lower()))
    return bool(tokens & PERSONALIZATION_TERMS)


def _candidate_strength(candidate: Mapping[str, object]) -> float:
    components = candidate.get("score_components", {})
    if not isinstance(components, Mapping):
        return 0.0
    return _clamp(
        0.35 * float(components.get("relationship", 0.0))
        + 0.25 * float(components.get("recency", 0.0))
        + 0.20 * float(components.get("semantic", 0.0))
        + 0.20 * float(components.get("behavioral", 0.0))
    )


def evidence_strength(
    decision: RawRoutingDecision,
    packet: RoutingPacket,
) -> float:
    """Compute the frozen evidence-strength formula from packet candidates."""

    payload = packet.as_dict()
    candidates = payload.get("historical_candidates", ())
    by_id = {
        candidate.get("message_id"): candidate
        for candidate in candidates
        if isinstance(candidate, Mapping)
    }
    selected_strengths = [
        _candidate_strength(by_id[message_id])
        for message_id in decision.selected_evidence_message_ids
        if message_id in by_id
    ]
    if selected_strengths:
        return _clamp(0.70 * max(selected_strengths) + 0.30 * sum(selected_strengths) / len(selected_strengths))
    if decision.message_type in CONTENT_INTRINSIC_TYPES or any(
        decision.semantic_flags.get(flag, False)
        for flag in (
            "credential_or_secret_request",
            "impersonation_or_domain_concern",
            "suspicious_link_or_payment_request",
            "warning_or_quoted_discussion",
        )
    ):
        return 0.50
    if _reason_claims_personalization(decision.reason):
        return 0.20
    return 0.35


def compute_contradiction_count(
    decision: RawRoutingDecision,
    message: SanitizedMessage,
) -> int:
    """Recompute independent contradictions without trusting model counts."""

    flags = decision.semantic_flags
    contradictions = 0
    if flags.get("warning_or_quoted_discussion") and flags.get(
        "credential_or_secret_request"
    ):
        contradictions += 1
    if flags.get("warning_or_quoted_discussion") and flags.get(
        "suspicious_link_or_payment_request"
    ):
        contradictions += 1
    if flags.get("transactional") and flags.get("promotional"):
        contradictions += 1
    if flags.get("time_critical"):
        deadline = decision.deadline_at
        if deadline is None or not (
            message.created_at < deadline <= message.created_at + timedelta(hours=2)
        ):
            contradictions += 1
    return contradictions


def _signal_agreement(contradiction_count: int) -> float:
    if contradiction_count <= 0:
        return 1.0
    if contradiction_count == 1:
        return 0.70
    if contradiction_count == 2:
        return 0.40
    return 0.10


def _extraction_quality(
    message: SanitizedMessage,
    packet: RoutingPacket,
    extraction_record: ExtractionRecord | None,
) -> float:
    if message.media_type is None:
        return 1.0
    media = packet.as_dict().get("media", {})
    state = media.get("media_state") if isinstance(media, Mapping) else None
    usable_message_text = _usable_text(message.message_text)
    if extraction_record is not None:
        score = _clamp(extraction_record.quality_score)
    else:
        score = 0.0
    if state == "ok":
        return max(score, 0.70)
    if state == "low_quality":
        return min(score, 0.55)
    if state == "empty_extraction":
        return 0.25 if usable_message_text else 0.05
    if state == "unsupported":
        return 0.20 if usable_message_text else 0.05
    if state == "decode_failed":
        return 0.15 if usable_message_text else 0.05
    if state == "missing":
        return 0.10 if usable_message_text else 0.05
    return 0.05


@dataclass(frozen=True, slots=True)
class ConfidenceAudit:
    """All deterministic confidence inputs and outputs for one decision."""

    policy_version: str
    routing_certainty: float
    extraction_quality: float
    evidence_strength: float
    context_completeness: float
    signal_agreement: float
    contradiction_count: int
    support: float
    base_confidence: float
    penalties: Mapping[str, float]
    penalty_total: float
    applied_cap: float | None
    unrounded_confidence: float
    rounded_confidence: float

    def as_dict(self) -> dict[str, object]:
        return {
            "confidence_policy_version": self.policy_version,
            "routing_certainty": self.routing_certainty,
            "extraction_quality": self.extraction_quality,
            "evidence_strength": self.evidence_strength,
            "context_completeness": self.context_completeness,
            "signal_agreement": self.signal_agreement,
            "contradiction_count": self.contradiction_count,
            "support": self.support,
            "base_confidence": self.base_confidence,
            "penalties": dict(self.penalties),
            "penalty_total": self.penalty_total,
            "applied_cap": self.applied_cap,
            "unrounded_confidence": self.unrounded_confidence,
            "rounded_confidence": self.rounded_confidence,
        }


def calculate_final_confidence(
    *,
    message: SanitizedMessage,
    packet: RoutingPacket,
    decision: RawRoutingDecision,
    features: DeterministicFeatures,
    extraction_record: ExtractionRecord | None,
    retrieval: RetrievalResult,
    routing_attempt_count: int,
    degraded: bool = False,
    safety_fallback: bool = False,
    contradiction_count: int | None = None,
) -> ConfidenceAudit:
    """Apply the frozen deterministic formula once, then round once."""

    del retrieval  # Candidate data is already canonicalized inside ``packet``.
    routing_certainty = _clamp(1.0 - decision.routing_uncertainty)
    extraction_quality = _extraction_quality(message, packet, extraction_record)
    evidence = evidence_strength(decision, packet)
    context = _clamp(
        sum(float(value) for value in features.context_components.values())
        / max(len(features.context_components), 1)
    )
    contradictions = (
        compute_contradiction_count(decision, message)
        if contradiction_count is None
        else max(0, int(contradiction_count))
    )
    agreement = _signal_agreement(contradictions)
    support = (
        0.30 * routing_certainty
        + 0.20 * extraction_quality
        + 0.20 * evidence
        + 0.20 * context
        + 0.10 * agreement
    )
    base_confidence = 0.05 + 0.93 * support
    penalties: dict[str, float] = {}
    if _reason_claims_personalization(decision.reason) and features.context_components.get(
        "relationship_context", 0.0
    ) < 1.0:
        penalties["critical_relationship_context_missing"] = 0.10
    if features.aggregate_snapshot_undefended:
        penalties["aggregate_snapshot_undefended"] = 0.08
    if contradictions == 1:
        penalties["one_semantic_contradiction"] = 0.08
    elif contradictions >= 2:
        penalties["multiple_semantic_contradictions"] = 0.18
    if routing_attempt_count > 1:
        penalties["routing_schema_or_constraint_retry"] = 0.06
    penalty_total = min(PENALTY_CAP, sum(penalties.values()))
    unrounded = _clamp(base_confidence - penalty_total, CONFIDENCE_FLOOR, CONFIDENCE_CEILING)
    caps: list[float] = []
    media_state = packet.as_dict().get("media", {}).get("media_state")
    usable_message_text = _usable_text(message.message_text)
    if media_state == "low_quality":
        caps.append(0.72)
    if media_state == "empty_extraction" and usable_message_text:
        caps.append(0.60)
    if media_state in {"missing", "unsupported", "decode_failed"} and usable_message_text:
        caps.append(0.55)
    if message.media_type is not None and media_state in {
        "missing",
        "unsupported",
        "decode_failed",
    } and not usable_message_text:
        caps.append(0.40)
    if contradictions >= 3:
        caps.append(0.35)
    if degraded:
        caps.append(0.10)
    if safety_fallback:
        caps.append(0.60)
    applied_cap = min(caps) if caps else None
    capped = min(unrounded, applied_cap) if applied_cap is not None else unrounded
    rounded = round(_clamp(capped, CONFIDENCE_FLOOR, CONFIDENCE_CEILING), 4)
    return ConfidenceAudit(
        policy_version=CONFIDENCE_POLICY_VERSION,
        routing_certainty=routing_certainty,
        extraction_quality=extraction_quality,
        evidence_strength=evidence,
        context_completeness=context,
        signal_agreement=agreement,
        contradiction_count=contradictions,
        support=support,
        base_confidence=base_confidence,
        penalties=penalties,
        penalty_total=penalty_total,
        applied_cap=applied_cap,
        unrounded_confidence=unrounded,
        rounded_confidence=rounded,
    )

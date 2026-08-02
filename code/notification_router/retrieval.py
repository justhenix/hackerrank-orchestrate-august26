"""Deterministic same-user historical retrieval and evidence provenance."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Mapping, Protocol

from .models import Message, NormalizedDataset


class IncomingMessageLike(Protocol):
    message_id: str
    user_id: str
    conversation_type: str
    group_id: str | None
    business_id: str | None
    sender_user_id: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    """Versioned non-embedding retrieval configuration."""

    version: str = "retrieval-non-embedding-v0"
    top_k: int = 12
    recency_window_days: int = 180
    same_scope_relationship: float = 1.0
    same_conversation_relationship: float = 0.4
    general_relationship: float = 0.2
    relationship_weight: float = 0.5
    recency_weight: float = 0.3
    behavioral_weight: float = 0.2
    content_summary_max_chars: int = 280

    def __post_init__(self) -> None:
        if not 0 <= self.top_k <= 12:
            raise ValueError("top_k must be between 0 and the contract maximum 12")
        if self.recency_window_days <= 0:
            raise ValueError("recency_window_days must be positive")
        if self.content_summary_max_chars <= 0:
            raise ValueError("content_summary_max_chars must be positive")
        weights = (
            self.relationship_weight,
            self.recency_weight,
            self.behavioral_weight,
        )
        if any(weight < 0 for weight in weights) or abs(sum(weights) - 1.0) > 1e-12:
            raise ValueError("fallback scoring weights must be non-negative and sum to one")

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "top_k": self.top_k,
            "recency_window_days": self.recency_window_days,
            "same_scope_relationship": self.same_scope_relationship,
            "same_conversation_relationship": self.same_conversation_relationship,
            "general_relationship": self.general_relationship,
            "relationship_weight": self.relationship_weight,
            "recency_weight": self.recency_weight,
            "behavioral_weight": self.behavioral_weight,
            "content_summary_max_chars": self.content_summary_max_chars,
            "semantic_mode": "disabled",
        }


@dataclass(frozen=True, slots=True)
class HistoricalCandidate:
    candidate_rank: int
    message_id: str
    user_id: str
    created_at: datetime
    relationship_scope: str
    content_summary: str
    event_summary: Mapping[str, bool]
    retrieval_score: float
    score_components: Mapping[str, float]

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_rank": self.candidate_rank,
            "message_id": self.message_id,
            "user_id": self.user_id,
            "created_at": self.created_at.isoformat(sep=" ", timespec="minutes"),
            "relationship_scope": self.relationship_scope,
            "content_summary": self.content_summary,
            "event_summary": dict(self.event_summary),
            "retrieval_score": self.retrieval_score,
            "score_components": dict(self.score_components),
        }


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    candidates: tuple[HistoricalCandidate, ...]
    allowed_evidence_message_ids: tuple[str, ...]
    config: RetrievalConfig

    def as_dict(self) -> dict[str, object]:
        return {
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "allowed_evidence_message_ids": list(self.allowed_evidence_message_ids),
            "config": self.config.as_dict(),
        }


class EvidenceProvenanceError(ValueError):
    """Raised when evidence violates the deterministic provenance boundary."""


def _relationship(
    incoming: IncomingMessageLike, historical: Message, config: RetrievalConfig
) -> tuple[str, float]:
    if incoming.business_id and incoming.business_id == historical.business_id:
        return "same_business", config.same_scope_relationship
    if incoming.group_id and incoming.group_id == historical.group_id:
        return "same_group", config.same_scope_relationship
    if incoming.sender_user_id and incoming.sender_user_id == historical.sender_user_id:
        return "same_sender", config.same_scope_relationship
    if incoming.conversation_type == historical.conversation_type:
        return "same_user_general", config.same_conversation_relationship
    return "same_user_general", config.general_relationship


def _behavioral_score(event: object | None) -> tuple[float, dict[str, bool]]:
    if event is None:
        return (
            0.3,
            {
                "opened": False,
                "replied": False,
                "dismissed": False,
                "muted_after": False,
                "reported": False,
            },
        )
    summary = {
        "opened": bool(event.message_opened),
        "replied": bool(event.message_replied),
        "dismissed": bool(event.notification_dismissed),
        "muted_after": bool(event.muted_after_message),
        "reported": bool(event.message_reported),
    }
    if summary["replied"] or summary["reported"]:
        return 1.0, summary
    if summary["muted_after"] or summary["dismissed"]:
        return 0.8, summary
    if summary["opened"]:
        return 0.6, summary
    return 0.3, summary


def _timestamp_sort_key(value: datetime) -> tuple[int, int, int, int, int, int]:
    return (
        -value.toordinal(),
        -value.hour,
        -value.minute,
        -value.second,
        -value.microsecond,
        0,
    )


def _candidate_sort_key(candidate: HistoricalCandidate) -> tuple[float, tuple[int, ...], str]:
    return (
        -candidate.retrieval_score,
        _timestamp_sort_key(candidate.created_at),
        candidate.message_id,
    )


def _bounded_summary(text: str, maximum: int) -> str:
    if len(text) <= maximum:
        return text
    return text[:maximum]


def retrieve_history(
    incoming: IncomingMessageLike,
    normalized: NormalizedDataset,
    config: RetrievalConfig | None = None,
) -> RetrievalResult:
    """Rank only same-user history that is strictly earlier than the input."""

    config = config or RetrievalConfig()
    candidates: list[HistoricalCandidate] = []
    for historical in normalized.strictly_prior_history(incoming):
        if historical.user_id != incoming.user_id:
            continue
        relationship_scope, relationship = _relationship(incoming, historical, config)
        age_days = (incoming.created_at - historical.created_at).total_seconds() / 86400
        recency = max(0.0, 1.0 - age_days / config.recency_window_days)
        behavioral, event_summary = _behavioral_score(
            normalized.events_by_message.get(historical.message_id)
        )
        score = (
            config.relationship_weight * relationship
            + config.recency_weight * recency
            + config.behavioral_weight * behavioral
        )
        candidates.append(
            HistoricalCandidate(
                candidate_rank=0,
                message_id=historical.message_id,
                user_id=historical.user_id,
                created_at=historical.created_at,
                relationship_scope=relationship_scope,
                content_summary=_bounded_summary(
                    historical.message_text, config.content_summary_max_chars
                ),
                event_summary=MappingProxyType(event_summary),
                retrieval_score=round(score, 12),
                score_components=MappingProxyType(
                    {
                        "relationship": round(relationship, 12),
                        "recency": round(recency, 12),
                        "semantic": 0.0,
                        "behavioral": round(behavioral, 12),
                    }
                ),
            )
        )
    ordered = tuple(
        replace(candidate, candidate_rank=index)
        for index, candidate in enumerate(
            sorted(candidates, key=_candidate_sort_key)[: config.top_k], start=1
        )
    )
    result = RetrievalResult(
        candidates=ordered,
        allowed_evidence_message_ids=tuple(candidate.message_id for candidate in ordered),
        config=config,
    )
    validate_evidence_allowlist(result, incoming, normalized)
    return result


def validate_evidence_allowlist(
    result: RetrievalResult,
    incoming: IncomingMessageLike,
    normalized: NormalizedDataset,
) -> None:
    """Fail closed if any candidate or allowlist ID violates provenance."""

    history_by_id = {message.message_id: message for message in normalized.history}
    candidate_ids = tuple(candidate.message_id for candidate in result.candidates)
    if candidate_ids != result.allowed_evidence_message_ids:
        raise EvidenceProvenanceError("allowlist order must equal candidate order")
    candidate_ranks = tuple(candidate.candidate_rank for candidate in result.candidates)
    if candidate_ranks != tuple(range(1, len(candidate_ranks) + 1)):
        raise EvidenceProvenanceError("candidate ranks must be contiguous and canonical")
    if len(candidate_ids) > result.config.top_k:
        raise EvidenceProvenanceError("candidate list exceeds configured top-K")
    if len(set(candidate_ids)) != len(candidate_ids):
        raise EvidenceProvenanceError("allowlist contains duplicate IDs")
    for candidate in result.candidates:
        historical = history_by_id.get(candidate.message_id)
        if historical is None:
            raise EvidenceProvenanceError("candidate ID is not in message history")
        if candidate.user_id != incoming.user_id or historical.user_id != incoming.user_id:
            raise EvidenceProvenanceError("candidate crosses the receiving-user boundary")
        if not historical.created_at < incoming.created_at:
            raise EvidenceProvenanceError("candidate is not strictly prior")
        if candidate.created_at != historical.created_at:
            raise EvidenceProvenanceError("candidate timestamp is not sourced from history")


def validate_selected_evidence(
    selected_ids: tuple[str, ...] | list[str],
    allowlist: tuple[str, ...] | list[str],
    *,
    evidence_limit: int = 3,
) -> tuple[str, ...]:
    """Validate a proposed subset without silently dropping or reordering IDs."""

    selected = tuple(selected_ids)
    allowed = tuple(allowlist)
    if len(set(allowed)) != len(allowed):
        raise EvidenceProvenanceError("allowlist contains duplicate IDs")
    if len(selected) > evidence_limit:
        raise EvidenceProvenanceError("selected evidence exceeds the configured limit")
    if len(set(selected)) != len(selected):
        raise EvidenceProvenanceError("selected evidence contains duplicate IDs")
    positions = {message_id: index for index, message_id in enumerate(allowed)}
    if any(message_id not in positions for message_id in selected):
        raise EvidenceProvenanceError("selected evidence is outside the allowlist")
    if tuple(sorted(selected, key=positions.__getitem__)) != selected:
        raise EvidenceProvenanceError("selected evidence must preserve allowlist order")
    return selected


# Short aliases make the deterministic boundary discoverable to callers.
retrieve_candidates = retrieve_history
build_evidence_allowlist = retrieve_history

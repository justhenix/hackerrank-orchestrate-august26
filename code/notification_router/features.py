"""Deterministic S4 features and initial action constraints.

This module consumes only sanitized incoming rows and participant context
tables.  Model-derived semantic flags are intentionally handled by the S8
finalizer, never by feature computation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from types import MappingProxyType
from typing import Mapping

from .inputs import SanitizedMessage
from .models import DatasetTables, NormalizedDataset


FEATURES_VERSION = "features-safety-v0"
MENTION_BOUNDARY_RE = r"[A-Za-z0-9_]"


@dataclass(frozen=True, slots=True)
class SafetyConstraints:
    """Deterministic constraints available before model interpretation."""

    allowed_actions: tuple[str, ...]
    required_action: str | None
    prohibited_actions: tuple[str, ...]
    triggered_invariants: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed_actions": list(self.allowed_actions),
            "required_action": self.required_action,
            "prohibited_actions": list(self.prohibited_actions),
            "triggered_invariants": list(self.triggered_invariants),
        }


@dataclass(frozen=True, slots=True)
class DeterministicFeatures:
    """Label-free, immutable features used by S4, S8, and confidence."""

    version: str
    explicit_user_id_mention: bool
    explicit_user_id_mention_sources: tuple[Mapping[str, object], ...]
    quiet_hours: bool
    group_muted: bool
    domain_mismatch: bool
    young_sender_domain: bool
    high_business_reports: bool
    prior_same_sender_interaction: bool
    prior_same_sender_report: bool
    trusted_business: bool
    trusted_group_sender: bool
    trusted_personal_sender: bool
    business_relationship_present: bool
    promotion_opt_out_at: datetime | None
    context_components: Mapping[str, float]
    missing_context: tuple[str, ...]
    aggregate_snapshot_undefended: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "feature_version": self.version,
            "explicit_user_id_mention": self.explicit_user_id_mention,
            "explicit_user_id_mention_sources": [
                dict(source) for source in self.explicit_user_id_mention_sources
            ],
            "quiet_hours": self.quiet_hours,
            "group_muted": self.group_muted,
            "domain_mismatch": self.domain_mismatch,
            "young_sender_domain": self.young_sender_domain,
            "high_business_reports": self.high_business_reports,
            "prior_same_sender_interaction": self.prior_same_sender_interaction,
            "prior_same_sender_report": self.prior_same_sender_report,
            "trusted_business": self.trusted_business,
            "trusted_group_sender": self.trusted_group_sender,
            "trusted_personal_sender": self.trusted_personal_sender,
            "business_relationship_present": self.business_relationship_present,
            "promotion_opt_out_at": (
                self.promotion_opt_out_at.isoformat()
                if self.promotion_opt_out_at is not None
                else None
            ),
            "context_components": dict(self.context_components),
            "missing_context": list(self.missing_context),
            "aggregate_snapshot_undefended": self.aggregate_snapshot_undefended,
        }


def _normalize_domain(value: str | None) -> str:
    if not value or not value.strip():
        return ""
    normalized = value.strip().lower().rstrip(".")
    try:
        return normalized.encode("idna").decode("ascii")
    except UnicodeError:
        return normalized


def _in_quiet_hours(value: time, start: time, end: time) -> bool:
    if start < end:
        return start <= value < end
    return value >= start or value < end


def _explicit_mentions(message: SanitizedMessage) -> tuple[Mapping[str, object], ...]:
    pattern = re.compile(
        rf"(?<!{MENTION_BOUNDARY_RE})@{re.escape(message.user_id)}(?!{MENTION_BOUNDARY_RE})"
    )
    return tuple(
        MappingProxyType(
            {
                "source_field": "message_text",
                "start_char": match.start(),
                "end_char_exclusive": match.end(),
            }
        )
        for match in pattern.finditer(message.message_text)
    )


def _prior_interactions(
    message: SanitizedMessage, normalized: NormalizedDataset
) -> tuple[bool, bool]:
    interacted = False
    reported = False
    for historical in normalized.strictly_prior_history(message):
        same_scope = (
            (message.sender_user_id is not None and historical.sender_user_id == message.sender_user_id)
            or (message.business_id is not None and historical.business_id == message.business_id)
            or (message.group_id is not None and historical.group_id == message.group_id)
        )
        if not same_scope:
            continue
        event = normalized.events_by_message.get(historical.message_id)
        if event is None:
            continue
        interacted = interacted or event.message_opened or event.message_replied
        same_sender_or_business = (
            (message.sender_user_id is not None and historical.sender_user_id == message.sender_user_id)
            or (message.business_id is not None and historical.business_id == message.business_id)
        )
        reported = reported or (same_sender_or_business and event.message_reported)
    return interacted, reported


def compute_deterministic_features(
    message: SanitizedMessage,
    tables: DatasetTables,
    normalized: NormalizedDataset,
) -> tuple[DeterministicFeatures, SafetyConstraints]:
    """Compute generalized label-free features and pre-routing constraints."""

    users = {row.user_id: row for row in tables.users}
    groups = {row.group_id: row for row in tables.groups}
    members = {(row.group_id, row.user_id): row for row in tables.group_members}
    businesses = {row.business_id: row for row in tables.business_accounts}
    business_history = {
        (row.user_id, row.business_id): row for row in tables.user_business_history
    }
    user = users.get(message.user_id)
    if user is None:
        raise ValueError("receiving user context is unavailable")
    group = groups.get(message.group_id) if message.group_id else None
    member = members.get((message.group_id, message.user_id)) if message.group_id else None
    business = businesses.get(message.business_id) if message.business_id else None
    relation = (
        business_history.get((message.user_id, message.business_id))
        if message.business_id
        else None
    )
    sender = users.get(message.sender_user_id) if message.sender_user_id else None

    mention_sources = _explicit_mentions(message)
    quiet_hours = _in_quiet_hours(
        message.created_at.time(),
        user.do_not_disturb_window.start,
        user.do_not_disturb_window.end,
    )
    group_muted = bool(member and member.group_muted_by_user)
    domain_mismatch = False
    young_sender_domain = False
    high_business_reports = False
    if business is not None:
        official_domain = _normalize_domain(business.official_domain)
        sender_domain = _normalize_domain(business.domain_used_by_sender)
        domain_mismatch = bool(official_domain and sender_domain and official_domain != sender_domain)
        young_sender_domain = business.domain_used_by_sender_age_days <= 30
        high_business_reports = (
            business.user_reports_30d >= 5
            and business.user_reports_30d / max(business.messages_sent_30d, 1) >= 0.02
        )
    prior_interaction, prior_report = _prior_interactions(message, normalized)
    trusted_business = bool(
        business
        and business.verified
        and not domain_mismatch
        and relation
        and relation.activity_count_180d >= 1
    )
    trusted_group_sender = bool(
        member
        and member.role == "admin"
        and message.group_id
        and message.sender_user_id
    )
    trusted_personal_sender = bool(prior_interaction and not prior_report)

    missing_context: list[str] = []
    if message.conversation_type == "group" and group is None:
        missing_context.append("group")
    if message.conversation_type == "group" and member is None:
        missing_context.append("group_member")
    if message.conversation_type == "business" and business is None:
        missing_context.append("business")
    if message.business_id and relation is None:
        missing_context.append("business_history")
    if message.sender_user_id and sender is None:
        missing_context.append("sender_user")

    conversation_context = 1.0
    if message.conversation_type == "group":
        conversation_context = 1.0 if group is not None else 0.0
    elif message.conversation_type == "business":
        conversation_context = 1.0 if business is not None else 0.0
    relationship_context = 1.0
    if message.conversation_type == "personal":
        relationship_context = 1.0 if sender is not None else 0.0
    elif message.conversation_type == "group":
        relationship_context = 1.0 if member is not None else (0.5 if group else 0.0)
    elif message.conversation_type == "business":
        relationship_context = 1.0 if relation is not None else (0.5 if business else 0.0)
    prior_event_count = sum(
        1
        for historical in normalized.strictly_prior_history(message)
        if normalized.events_by_message.get(historical.message_id) is not None
    )
    prior_behavior_context = 0.5 if prior_event_count else 0.0
    if normalized.events_by_message and user.user_id:
        prior_behavior_context += 0.5 if prior_event_count else 0.0
    if normalized.contexts:
        # Development inputs are deliberately not inserted into ``contexts``;
        # this branch only records that the normalized context index exists.
        prior_behavior_context = min(1.0, prior_behavior_context)
    context_components = MappingProxyType(
        {
            "user_context": 1.0,
            "conversation_context": conversation_context,
            "relationship_context": relationship_context,
            "prior_behavior_context": prior_behavior_context,
        }
    )

    features = DeterministicFeatures(
        version=FEATURES_VERSION,
        explicit_user_id_mention=bool(mention_sources),
        explicit_user_id_mention_sources=mention_sources,
        quiet_hours=quiet_hours,
        group_muted=group_muted,
        domain_mismatch=domain_mismatch,
        young_sender_domain=young_sender_domain,
        high_business_reports=high_business_reports,
        prior_same_sender_interaction=prior_interaction,
        prior_same_sender_report=prior_report,
        trusted_business=trusted_business,
        trusted_group_sender=trusted_group_sender,
        trusted_personal_sender=trusted_personal_sender,
        business_relationship_present=relation is not None,
        promotion_opt_out_at=(relation.promotions_opted_out_at if relation else None),
        context_components=context_components,
        missing_context=tuple(sorted(set(missing_context))),
        # Aggregate 30-day/180-day snapshots have no per-message as-of time.
        aggregate_snapshot_undefended=True,
    )
    prohibited: list[str] = []
    invariants: list[str] = []
    if quiet_hours:
        prohibited.append("notify")
        invariants.append("INV-103")
    if group_muted:
        prohibited.append("notify")
        invariants.append("INV-104")
    constraints = SafetyConstraints(
        allowed_actions=("notify", "digest", "mute"),
        required_action=None,
        prohibited_actions=tuple(dict.fromkeys(prohibited)),
        triggered_invariants=tuple(invariants),
    )
    return features, constraints

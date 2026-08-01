"""Typed immutable rows and normalized join views for the input contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class QuietHours:
    start: time
    end: time

    @property
    def crosses_midnight(self) -> bool:
        return self.start >= self.end


@dataclass(frozen=True, slots=True)
class Message:
    message_id: str
    user_id: str
    conversation_type: str
    group_id: str | None
    business_id: str | None
    sender_user_id: str | None
    created_at: datetime
    message_text: str
    media_type: str | None
    media_id: str | None
    forwarded_count: int


@dataclass(frozen=True, slots=True)
class User:
    user_id: str
    do_not_disturb_window: QuietHours
    messages_opened_30d: int
    messages_replied_30d: int
    notifications_dismissed_30d: int
    messages_reported_30d: int


@dataclass(frozen=True, slots=True)
class Group:
    group_id: str
    group_name: str
    group_type: str
    member_count: int
    admin_count: int
    created_at: date
    messages_30d: int


@dataclass(frozen=True, slots=True)
class GroupMember:
    group_id: str
    user_id: str
    role: str
    joined_at: date
    messages_sent_30d: int
    messages_read_30d: int
    replies_sent_30d: int
    notifications_dismissed_30d: int
    group_muted_by_user: bool


@dataclass(frozen=True, slots=True)
class BusinessAccount:
    business_id: str
    display_name: str
    brand_name: str
    category: str
    verified: bool
    official_domain: str | None
    domain_used_by_sender: str | None
    account_age_days: int
    messages_sent_30d: int
    user_reports_30d: int
    domain_used_by_sender_age_days: int


@dataclass(frozen=True, slots=True)
class UserBusinessHistory:
    user_id: str
    business_id: str
    why_user_knows_account: str
    last_activity_at: datetime
    allows_promotions: bool
    promotions_opted_out_at: datetime | None
    activity_count_180d: int
    messages_opened_30d: int
    messages_dismissed_30d: int
    messages_replied_30d: int
    last_reply_at: datetime | None


@dataclass(frozen=True, slots=True)
class MessageEvent:
    user_id: str
    message_id: str
    message_opened: bool
    message_replied: bool
    reaction_time_minutes: int | None
    notification_dismissed: bool
    muted_after_message: bool
    message_reported: bool


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    image_id: str
    file_path: str


@dataclass(frozen=True, slots=True)
class VoiceNoteMetadata:
    voice_note_id: str
    file_path: str


@dataclass(frozen=True, slots=True)
class DailyNotificationSummary:
    user_id: str
    date: date
    notifications_sent: int
    notifications_dismissed: int


@dataclass(frozen=True, slots=True)
class DatasetTables:
    """All participant-facing runtime tables, loaded in source row order."""

    dataset_root: Path
    timestamp_policy: str
    messages: tuple[Message, ...]
    users: tuple[User, ...]
    groups: tuple[Group, ...]
    group_members: tuple[GroupMember, ...]
    business_accounts: tuple[BusinessAccount, ...]
    user_business_history: tuple[UserBusinessHistory, ...]
    message_history: tuple[Message, ...]
    message_events: tuple[MessageEvent, ...]
    images: tuple[ImageMetadata, ...]
    voice_notes: tuple[VoiceNoteMetadata, ...]
    daily_notification_summary: tuple[DailyNotificationSummary, ...]


@dataclass(frozen=True, slots=True)
class MessageContext:
    """One incoming message with explicit, nullable relationship joins."""

    message: Message
    user: User
    group: Group | None
    group_member: GroupMember | None
    sender: User | None
    business: BusinessAccount | None
    business_history: UserBusinessHistory | None
    daily_summary: DailyNotificationSummary | None


@dataclass(frozen=True, slots=True)
class JoinCoverage:
    total_messages: int
    users_joined: int
    groups_joined: int
    group_memberships_joined: int
    sender_users_joined: int
    businesses_joined: int
    business_histories_joined: int
    daily_summaries_joined: int
    optional_missing: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class NormalizedDataset:
    """Immutable lookup indexes and strictly-prior history views."""

    dataset_root: Path
    timestamp_policy: str
    messages: tuple[Message, ...]
    history: tuple[Message, ...]
    contexts: Mapping[str, MessageContext]
    history_by_user: Mapping[str, tuple[Message, ...]]
    events_by_message: Mapping[str, MessageEvent]
    join_coverage: JoinCoverage

    def context_for(self, message_id: str) -> MessageContext:
        return self.contexts[message_id]

    def strictly_prior_history(self, message: Message | MessageContext) -> tuple[Message, ...]:
        """Return same-user history with ``created_at < incoming.created_at`` only."""

        incoming = message.message if isinstance(message, MessageContext) else message
        return tuple(
            historical
            for historical in self.history_by_user.get(incoming.user_id, ())
            if historical.created_at < incoming.created_at
        )

    def strictly_prior_history_for(self, message_id: str) -> tuple[Message, ...]:
        return self.strictly_prior_history(self.context_for(message_id))

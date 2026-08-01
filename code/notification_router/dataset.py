"""Deterministic loading, integrity validation, normalized joins, and history views."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from .errors import DatasetValidationError, ValidationIssue
from .models import (
    BusinessAccount,
    DailyNotificationSummary,
    DatasetTables,
    Group,
    GroupMember,
    ImageMetadata,
    JoinCoverage,
    Message,
    MessageContext,
    MessageEvent,
    NormalizedDataset,
    User,
    UserBusinessHistory,
    VoiceNoteMetadata,
)
from .schemas import REQUIRED_RUNTIME_FILES, load_csv_table


DATASET_WALL_CLOCK_POLICY = "dataset-local-naive-wall-clock"


def _message(row: Mapping[str, object]) -> Message:
    return Message(
        message_id=row["message_id"],
        user_id=row["user_id"],
        conversation_type=row["conversation_type"],
        group_id=row["group_id"],
        business_id=row["business_id"],
        sender_user_id=row["sender_user_id"],
        created_at=row["created_at"],
        message_text=row["message_text"],
        media_type=row["media_type"],
        media_id=row["media_id"],
        forwarded_count=row["forwarded_count"],
    )


def _convert_rows(
    raw: Mapping[str, tuple[Mapping[str, object], ...]],
    dataset_root: Path,
) -> DatasetTables:
    return DatasetTables(
        dataset_root=dataset_root,
        timestamp_policy=DATASET_WALL_CLOCK_POLICY,
        messages=tuple(_message(row) for row in raw["messages.csv"]),
        users=tuple(
            User(
                user_id=row["user_id"],
                do_not_disturb_window=row["do_not_disturb_window"],
                messages_opened_30d=row["messages_opened_30d"],
                messages_replied_30d=row["messages_replied_30d"],
                notifications_dismissed_30d=row["notifications_dismissed_30d"],
                messages_reported_30d=row["messages_reported_30d"],
            )
            for row in raw["users.csv"]
        ),
        groups=tuple(
            Group(
                group_id=row["group_id"],
                group_name=row["group_name"],
                group_type=row["group_type"],
                member_count=row["member_count"],
                admin_count=row["admin_count"],
                created_at=row["created_at"],
                messages_30d=row["messages_30d"],
            )
            for row in raw["groups.csv"]
        ),
        group_members=tuple(
            GroupMember(
                group_id=row["group_id"],
                user_id=row["user_id"],
                role=row["role"],
                joined_at=row["joined_at"],
                messages_sent_30d=row["messages_sent_30d"],
                messages_read_30d=row["messages_read_30d"],
                replies_sent_30d=row["replies_sent_30d"],
                notifications_dismissed_30d=row["notifications_dismissed_30d"],
                group_muted_by_user=row["group_muted_by_user"],
            )
            for row in raw["group_members.csv"]
        ),
        business_accounts=tuple(
            BusinessAccount(
                business_id=row["business_id"],
                display_name=row["display_name"],
                brand_name=row["brand_name"],
                category=row["category"],
                verified=row["verified"],
                official_domain=row["official_domain"],
                domain_used_by_sender=row["domain_used_by_sender"],
                account_age_days=row["account_age_days"],
                messages_sent_30d=row["messages_sent_30d"],
                user_reports_30d=row["user_reports_30d"],
                domain_used_by_sender_age_days=row["domain_used_by_sender_age_days"],
            )
            for row in raw["business_accounts.csv"]
        ),
        user_business_history=tuple(
            UserBusinessHistory(
                user_id=row["user_id"],
                business_id=row["business_id"],
                why_user_knows_account=row["why_user_knows_account"],
                last_activity_at=row["last_activity_at"],
                allows_promotions=row["allows_promotions"],
                promotions_opted_out_at=row["promotions_opted_out_at"],
                activity_count_180d=row["activity_count_180d"],
                messages_opened_30d=row["messages_opened_30d"],
                messages_dismissed_30d=row["messages_dismissed_30d"],
                messages_replied_30d=row["messages_replied_30d"],
                last_reply_at=row["last_reply_at"],
            )
            for row in raw["user_business_history.csv"]
        ),
        message_history=tuple(_message(row) for row in raw["message_history.csv"]),
        message_events=tuple(
            MessageEvent(
                user_id=row["user_id"],
                message_id=row["message_id"],
                message_opened=row["message_opened"],
                message_replied=row["message_replied"],
                reaction_time_minutes=row["reaction_time_minutes"],
                notification_dismissed=row["notification_dismissed"],
                muted_after_message=row["muted_after_message"],
                message_reported=row["message_reported"],
            )
            for row in raw["message_events.csv"]
        ),
        images=tuple(
            ImageMetadata(image_id=row["image_id"], file_path=row["file_path"])
            for row in raw["images.csv"]
        ),
        voice_notes=tuple(
            VoiceNoteMetadata(
                voice_note_id=row["voice_note_id"], file_path=row["file_path"]
            )
            for row in raw["voice_notes.csv"]
        ),
        daily_notification_summary=tuple(
            DailyNotificationSummary(
                user_id=row["user_id"],
                date=row["date"],
                notifications_sent=row["notifications_sent"],
                notifications_dismissed=row["notifications_dismissed"],
            )
            for row in raw["daily_notification_summary.csv"]
        ),
    )


def _duplicate_keys(
    table: str, rows: Iterable[object], fields: tuple[str, ...]
) -> list[ValidationIssue]:
    seen: dict[tuple[object, ...], int] = {}
    issues: list[ValidationIssue] = []
    for row_number, row in enumerate(rows, start=2):
        key = tuple(getattr(row, field) for field in fields)
        if key in seen:
            issues.append(
                ValidationIssue(
                    "DUPLICATE_KEY",
                    table=table,
                    row_number=row_number,
                    detail=f"duplicate key; first seen at row {seen[key]}",
                )
            )
        else:
            seen[key] = row_number
    return issues


def validate_key_integrity(tables: DatasetTables) -> list[ValidationIssue]:
    """Validate primary/composite uniqueness, including target/history separation."""

    specifications = (
        ("messages.csv", tables.messages, ("message_id",)),
        ("users.csv", tables.users, ("user_id",)),
        ("groups.csv", tables.groups, ("group_id",)),
        ("group_members.csv", tables.group_members, ("group_id", "user_id")),
        ("business_accounts.csv", tables.business_accounts, ("business_id",)),
        (
            "user_business_history.csv",
            tables.user_business_history,
            ("user_id", "business_id"),
        ),
        ("message_history.csv", tables.message_history, ("message_id",)),
        ("message_events.csv", tables.message_events, ("user_id", "message_id")),
        ("images.csv", tables.images, ("image_id",)),
        ("voice_notes.csv", tables.voice_notes, ("voice_note_id",)),
        (
            "daily_notification_summary.csv",
            tables.daily_notification_summary,
            ("user_id", "date"),
        ),
    )
    issues: list[ValidationIssue] = []
    for table, rows, fields in specifications:
        issues.extend(_duplicate_keys(table, rows, fields))

    target_ids = {row.message_id for row in tables.messages}
    history_ids = {row.message_id for row in tables.message_history}
    for message_id in sorted(target_ids & history_ids):
        issues.append(
            ValidationIssue(
                "DUPLICATE_KEY",
                table="messages.csv+message_history.csv",
                detail=f"message_id appears in both tables: {message_id}",
            )
        )
    return issues


def _safe_relative_path(root: Path, value: str) -> bool:
    candidate = Path(value)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        return False
    try:
        (root / candidate).resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _check_reference(
    issues: list[ValidationIssue],
    *,
    table: str,
    row_number: int,
    field: str,
    value: object,
    allowed: set[object],
    required: bool = False,
) -> None:
    if value is None:
        if required:
            issues.append(
                ValidationIssue(
                    "CONDITIONAL_FIELD_INVALID",
                    table=table,
                    row_number=row_number,
                    field=field,
                    detail="required reference is empty",
                )
            )
        return
    if value not in allowed:
        issues.append(
            ValidationIssue(
                "REFERENCE_BROKEN",
                table=table,
                row_number=row_number,
                field=field,
                detail="referenced key does not exist",
            )
        )


def _validate_message_row(
    message: Message,
    *,
    table: str,
    row_number: int,
    user_ids: set[str],
    group_ids: set[str],
    business_ids: set[str],
    image_ids: set[str],
    voice_ids: set[str],
    issues: list[ValidationIssue],
) -> None:
    _check_reference(
        issues,
        table=table,
        row_number=row_number,
        field="user_id",
        value=message.user_id,
        allowed=user_ids,
        required=True,
    )
    if message.conversation_type == "group":
        _check_reference(
            issues,
            table=table,
            row_number=row_number,
            field="group_id",
            value=message.group_id,
            allowed=group_ids,
            required=True,
        )
        _check_reference(
            issues,
            table=table,
            row_number=row_number,
            field="sender_user_id",
            value=message.sender_user_id,
            allowed=user_ids,
            required=True,
        )
        if message.business_id is not None:
            issues.append(
                ValidationIssue(
                    "CONDITIONAL_FIELD_INVALID",
                    table=table,
                    row_number=row_number,
                    field="business_id",
                    detail="must be empty for group conversations",
                )
            )
    elif message.conversation_type == "personal":
        _check_reference(
            issues,
            table=table,
            row_number=row_number,
            field="sender_user_id",
            value=message.sender_user_id,
            allowed=user_ids,
            required=True,
        )
        for field, value in (("group_id", message.group_id), ("business_id", message.business_id)):
            if value is not None:
                issues.append(
                    ValidationIssue(
                        "CONDITIONAL_FIELD_INVALID",
                        table=table,
                        row_number=row_number,
                        field=field,
                        detail="must be empty for personal conversations",
                    )
                )
    elif message.conversation_type == "business":
        _check_reference(
            issues,
            table=table,
            row_number=row_number,
            field="business_id",
            value=message.business_id,
            allowed=business_ids,
            required=True,
        )
        for field, value in (("group_id", message.group_id), ("sender_user_id", message.sender_user_id)):
            if value is not None:
                issues.append(
                    ValidationIssue(
                        "CONDITIONAL_FIELD_INVALID",
                        table=table,
                        row_number=row_number,
                        field=field,
                        detail="must be empty for business conversations",
                    )
                )

    if message.media_type == "image":
        _check_reference(
            issues,
            table=table,
            row_number=row_number,
            field="media_id",
            value=message.media_id,
            allowed=image_ids,
            required=True,
        )
    elif message.media_type == "voice":
        _check_reference(
            issues,
            table=table,
            row_number=row_number,
            field="media_id",
            value=message.media_id,
            allowed=voice_ids,
            required=True,
        )
    elif message.media_id is not None:
        issues.append(
            ValidationIssue(
                "CONDITIONAL_FIELD_INVALID",
                table=table,
                row_number=row_number,
                field="media_id",
                detail="must be empty when media_type is empty",
            )
        )


def validate_referential_integrity(tables: DatasetTables) -> list[ValidationIssue]:
    """Validate all declared foreign keys and conditional relationship fields."""

    user_ids = {row.user_id for row in tables.users}
    group_ids = {row.group_id for row in tables.groups}
    business_ids = {row.business_id for row in tables.business_accounts}
    image_ids = {row.image_id for row in tables.images}
    voice_ids = {row.voice_note_id for row in tables.voice_notes}
    history_by_id = {row.message_id: row for row in tables.message_history}
    issues: list[ValidationIssue] = []

    for table, rows in (("messages.csv", tables.messages), ("message_history.csv", tables.message_history)):
        for row_number, message in enumerate(rows, start=2):
            _validate_message_row(
                message,
                table=table,
                row_number=row_number,
                user_ids=user_ids,
                group_ids=group_ids,
                business_ids=business_ids,
                image_ids=image_ids,
                voice_ids=voice_ids,
                issues=issues,
            )

    for row_number, member in enumerate(tables.group_members, start=2):
        _check_reference(
            issues,
            table="group_members.csv",
            row_number=row_number,
            field="group_id",
            value=member.group_id,
            allowed=group_ids,
            required=True,
        )
        _check_reference(
            issues,
            table="group_members.csv",
            row_number=row_number,
            field="user_id",
            value=member.user_id,
            allowed=user_ids,
            required=True,
        )

    for row_number, relation in enumerate(tables.user_business_history, start=2):
        _check_reference(
            issues,
            table="user_business_history.csv",
            row_number=row_number,
            field="user_id",
            value=relation.user_id,
            allowed=user_ids,
            required=True,
        )
        _check_reference(
            issues,
            table="user_business_history.csv",
            row_number=row_number,
            field="business_id",
            value=relation.business_id,
            allowed=business_ids,
            required=True,
        )

    for row_number, event in enumerate(tables.message_events, start=2):
        history = history_by_id.get(event.message_id)
        _check_reference(
            issues,
            table="message_events.csv",
            row_number=row_number,
            field="message_id",
            value=event.message_id,
            allowed=set(history_by_id),
            required=True,
        )
        _check_reference(
            issues,
            table="message_events.csv",
            row_number=row_number,
            field="user_id",
            value=event.user_id,
            allowed=user_ids,
            required=True,
        )
        if history is not None and history.user_id != event.user_id:
            issues.append(
                ValidationIssue(
                    "REFERENCE_BROKEN",
                    table="message_events.csv",
                    row_number=row_number,
                    field="user_id",
                    detail="event user does not match historical message user",
                )
            )

    for row_number, summary in enumerate(tables.daily_notification_summary, start=2):
        _check_reference(
            issues,
            table="daily_notification_summary.csv",
            row_number=row_number,
            field="user_id",
            value=summary.user_id,
            allowed=user_ids,
            required=True,
        )

    for table, rows in (("images.csv", tables.images), ("voice_notes.csv", tables.voice_notes)):
        for row_number, media in enumerate(rows, start=2):
            if not _safe_relative_path(tables.dataset_root, media.file_path):
                issues.append(
                    ValidationIssue(
                        "REFERENCE_BROKEN",
                        table=table,
                        row_number=row_number,
                        field="file_path",
                        detail="path must remain within the dataset directory",
                    )
                )
    return issues


def validate_dataset(tables: DatasetTables) -> list[ValidationIssue]:
    """Run key and foreign-key checks on an already typed dataset."""

    return validate_key_integrity(tables) + validate_referential_integrity(tables)


def load_dataset(dataset_dir: str | Path) -> DatasetTables:
    """Load the fixed participant allowlist and fail closed on contract errors."""

    root = Path(dataset_dir)
    issues: list[ValidationIssue] = []
    if not root.is_dir():
        raise DatasetValidationError(
            [ValidationIssue("INPUT_MISSING", table="dataset", detail="dataset directory does not exist")]
        )
    root = root.resolve()

    raw: dict[str, tuple[Mapping[str, object], ...]] = {}
    for filename in REQUIRED_RUNTIME_FILES:
        try:
            raw[filename] = load_csv_table(root / filename, filename)
        except DatasetValidationError as exc:
            issues.extend(exc.issues)
    if issues:
        raise DatasetValidationError(issues)

    tables = _convert_rows(raw, root)
    issues = validate_dataset(tables)
    if issues:
        raise DatasetValidationError(issues)
    return tables


def normalize_dataset(tables: DatasetTables) -> NormalizedDataset:
    """Build explicit nullable joins and deterministic history indexes."""

    issues = validate_dataset(tables)
    if issues:
        raise DatasetValidationError(issues)

    users = {row.user_id: row for row in tables.users}
    groups = {row.group_id: row for row in tables.groups}
    members = {(row.group_id, row.user_id): row for row in tables.group_members}
    businesses = {row.business_id: row for row in tables.business_accounts}
    business_history = {
        (row.user_id, row.business_id): row for row in tables.user_business_history
    }
    summaries = {
        (row.user_id, row.date): row for row in tables.daily_notification_summary
    }

    contexts: dict[str, MessageContext] = {}
    for message in tables.messages:
        context = MessageContext(
            message=message,
            user=users[message.user_id],
            group=groups.get(message.group_id) if message.group_id else None,
            group_member=(
                members.get((message.group_id, message.user_id))
                if message.group_id
                else None
            ),
            sender=users.get(message.sender_user_id) if message.sender_user_id else None,
            business=(
                businesses.get(message.business_id) if message.business_id else None
            ),
            business_history=(
                business_history.get((message.user_id, message.business_id))
                if message.business_id
                else None
            ),
            daily_summary=summaries.get((message.user_id, message.created_at.date())),
        )
        contexts[message.message_id] = context

    history_by_user_mutable: dict[str, list[Message]] = defaultdict(list)
    for message in tables.message_history:
        history_by_user_mutable[message.user_id].append(message)
    history_by_user = {
        user_id: tuple(sorted(rows, key=lambda row: (row.created_at, row.message_id)))
        for user_id, rows in history_by_user_mutable.items()
    }
    events_by_message = {event.message_id: event for event in tables.message_events}

    group_messages = sum(message.group_id is not None for message in tables.messages)
    business_messages = sum(message.business_id is not None for message in tables.messages)
    group_memberships = sum(context.group_member is not None for context in contexts.values())
    business_histories = sum(context.business_history is not None for context in contexts.values())
    daily_summaries = sum(context.daily_summary is not None for context in contexts.values())
    sender_messages = sum(message.sender_user_id is not None for message in tables.messages)
    sender_users = sum(context.sender is not None for context in contexts.values())
    coverage = JoinCoverage(
        total_messages=len(tables.messages),
        users_joined=len(contexts),
        groups_joined=sum(context.group is not None for context in contexts.values()),
        group_memberships_joined=group_memberships,
        sender_users_joined=sender_users,
        businesses_joined=sum(context.business is not None for context in contexts.values()),
        business_histories_joined=business_histories,
        daily_summaries_joined=daily_summaries,
        optional_missing=MappingProxyType(
            {
                "group_member": group_messages - group_memberships,
                "business_history": business_messages - business_histories,
                "daily_summary": len(tables.messages) - daily_summaries,
                "sender_user": sender_messages - sender_users,
            }
        ),
    )
    return NormalizedDataset(
        dataset_root=tables.dataset_root,
        timestamp_policy=tables.timestamp_policy,
        messages=tables.messages,
        history=tables.message_history,
        contexts=MappingProxyType(contexts),
        history_by_user=MappingProxyType(history_by_user),
        events_by_message=MappingProxyType(events_by_message),
        join_coverage=coverage,
    )

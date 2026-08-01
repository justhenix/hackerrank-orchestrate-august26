"""Exact CSV schemas and deterministic UTF-8 parsing."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from .errors import DatasetValidationError, ValidationIssue
from .models import QuietHours


ACTION_VALUES = frozenset({"notify", "digest", "mute"})
MESSAGE_TYPE_VALUES = frozenset(
    {
        "personal",
        "urgent",
        "event",
        "payment",
        "business_update",
        "promotion",
        "greeting",
        "forward",
        "spam",
        "scam",
        "unknown",
    }
)
CONVERSATION_VALUES = frozenset({"personal", "group", "business"})
MEDIA_VALUES = frozenset({"image", "voice"})
ROLE_VALUES = frozenset({"admin", "member"})


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    kind: str
    nullable: bool = False
    preserve_empty: bool = False
    enum_values: frozenset[str] = frozenset()
    minimum: int | None = None
    maximum: int | None = None

    def parse(self, raw: str) -> object:
        if self.preserve_empty and raw == "":
            return ""
        if raw == "":
            if self.nullable:
                return None
            raise ValueError("required value is empty")

        value = raw if self.preserve_empty else raw.strip()
        if value == "" and not self.preserve_empty:
            if self.nullable:
                return None
            raise ValueError("required value is blank")

        if self.kind in {"id", "text", "path"}:
            return value

        if self.kind == "enum":
            if value not in self.enum_values:
                raise EnumValueError(f"unexpected value {value!r}")
            return value

        if self.kind == "int":
            if not re.fullmatch(r"\d+", value):
                raise ValueError("expected a non-negative integer")
            parsed = int(value)
            if self.minimum is not None and parsed < self.minimum:
                raise ValueError(f"value is below minimum {self.minimum}")
            if self.maximum is not None and parsed > self.maximum:
                raise ValueError(f"value is above maximum {self.maximum}")
            return parsed

        if self.kind == "bool":
            if value not in {"0", "1"}:
                raise EnumValueError("boolean must be 0 or 1")
            return value == "1"

        if self.kind == "datetime":
            try:
                return datetime.strptime(value, "%Y-%m-%d %H:%M")
            except ValueError as exc:
                raise ValueError("expected YYYY-MM-DD HH:MM") from exc

        if self.kind == "date":
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("expected YYYY-MM-DD") from exc

        if self.kind == "time_window":
            match = re.fullmatch(
                r"(?P<start>[01]\d|2[0-3]):(?P<start_min>[0-5]\d)-"
                r"(?P<end>[01]\d|2[0-3]):(?P<end_min>[0-5]\d)",
                value,
            )
            if not match:
                raise ValueError("expected HH:MM-HH:MM")
            start = time(int(match.group("start")), int(match.group("start_min")))
            end = time(int(match.group("end")), int(match.group("end_min")))
            return QuietHours(start=start, end=end)

        if self.kind == "decimal":
            try:
                parsed = Decimal(value)
            except InvalidOperation as exc:
                raise ValueError("expected a decimal number") from exc
            if not parsed.is_finite():
                raise ValueError("decimal must be finite")
            if self.minimum is not None and parsed < self.minimum:
                raise ValueError(f"value is below minimum {self.minimum}")
            if self.maximum is not None and parsed > self.maximum:
                raise ValueError(f"value is above maximum {self.maximum}")
            return parsed

        raise ValueError(f"unknown field kind {self.kind!r}")


class EnumValueError(ValueError):
    """Marks a parse failure that is specifically an enum-domain violation."""


@dataclass(frozen=True, slots=True)
class TableSchema:
    filename: str
    fields: tuple[FieldSpec, ...]
    primary_key: tuple[str, ...]

    @property
    def columns(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)


def _id(name: str, *, nullable: bool = False) -> FieldSpec:
    return FieldSpec(name=name, kind="id", nullable=nullable)


def _text(
    name: str,
    *,
    nullable: bool = False,
    preserve_empty: bool = False,
    path: bool = False,
) -> FieldSpec:
    return FieldSpec(
        name=name,
        kind="path" if path else "text",
        nullable=nullable,
        preserve_empty=preserve_empty,
    )


def _enum(name: str, values: frozenset[str], *, nullable: bool = False) -> FieldSpec:
    return FieldSpec(name=name, kind="enum", nullable=nullable, enum_values=values)


def _int(name: str, *, nullable: bool = False, minimum: int = 0) -> FieldSpec:
    return FieldSpec(name=name, kind="int", nullable=nullable, minimum=minimum)


def _bool(name: str) -> FieldSpec:
    return FieldSpec(name=name, kind="bool")


def _datetime(name: str, *, nullable: bool = False) -> FieldSpec:
    return FieldSpec(name=name, kind="datetime", nullable=nullable)


def _date(name: str) -> FieldSpec:
    return FieldSpec(name=name, kind="date")


MESSAGE_FIELDS = (
    _id("message_id"),
    _id("user_id"),
    _enum("conversation_type", CONVERSATION_VALUES),
    _id("group_id", nullable=True),
    _id("business_id", nullable=True),
    _id("sender_user_id", nullable=True),
    _datetime("created_at"),
    _text("message_text", nullable=True, preserve_empty=True),
    _enum("media_type", MEDIA_VALUES, nullable=True),
    _id("media_id", nullable=True),
    _int("forwarded_count"),
)


OUTPUT_FIELDS = (
    _id("message_id"),
    _enum("action", ACTION_VALUES),
    _enum("message_type", MESSAGE_TYPE_VALUES),
    _text("reason"),
    FieldSpec(name="confidence", kind="decimal", minimum=0, maximum=1),
    _text("evidence_message_ids"),
)


def _schema(filename: str, fields: tuple[FieldSpec, ...], *primary_key: str) -> TableSchema:
    return TableSchema(filename=filename, fields=fields, primary_key=primary_key)


TABLE_SCHEMAS: Mapping[str, TableSchema] = MappingProxyType(
    {
        "messages.csv": _schema("messages.csv", MESSAGE_FIELDS, "message_id"),
        "message_history.csv": _schema(
            "message_history.csv", MESSAGE_FIELDS, "message_id"
        ),
        "users.csv": _schema(
            "users.csv",
            (
                _id("user_id"),
                FieldSpec("do_not_disturb_window", "time_window"),
                _int("messages_opened_30d"),
                _int("messages_replied_30d"),
                _int("notifications_dismissed_30d"),
                _int("messages_reported_30d"),
            ),
            "user_id",
        ),
        "groups.csv": _schema(
            "groups.csv",
            (
                _id("group_id"),
                _text("group_name"),
                _text("group_type"),
                _int("member_count"),
                _int("admin_count"),
                _date("created_at"),
                _int("messages_30d"),
            ),
            "group_id",
        ),
        "group_members.csv": _schema(
            "group_members.csv",
            (
                _id("group_id"),
                _id("user_id"),
                _enum("role", ROLE_VALUES),
                _date("joined_at"),
                _int("messages_sent_30d"),
                _int("messages_read_30d"),
                _int("replies_sent_30d"),
                _int("notifications_dismissed_30d"),
                _bool("group_muted_by_user"),
            ),
            "group_id",
            "user_id",
        ),
        "business_accounts.csv": _schema(
            "business_accounts.csv",
            (
                _id("business_id"),
                _text("display_name"),
                _text("brand_name"),
                _text("category"),
                _bool("verified"),
                _text("official_domain", nullable=True),
                _text("domain_used_by_sender", nullable=True),
                _int("account_age_days"),
                _int("messages_sent_30d"),
                _int("user_reports_30d"),
                _int("domain_used_by_sender_age_days"),
            ),
            "business_id",
        ),
        "user_business_history.csv": _schema(
            "user_business_history.csv",
            (
                _id("user_id"),
                _id("business_id"),
                _text("why_user_knows_account"),
                _datetime("last_activity_at"),
                _bool("allows_promotions"),
                _datetime("promotions_opted_out_at", nullable=True),
                _int("activity_count_180d"),
                _int("messages_opened_30d"),
                _int("messages_dismissed_30d"),
                _int("messages_replied_30d"),
                _datetime("last_reply_at", nullable=True),
            ),
            "user_id",
            "business_id",
        ),
        "message_events.csv": _schema(
            "message_events.csv",
            (
                _id("user_id"),
                _id("message_id"),
                _bool("message_opened"),
                _bool("message_replied"),
                _int("reaction_time_minutes", nullable=True),
                _bool("notification_dismissed"),
                _bool("muted_after_message"),
                _bool("message_reported"),
            ),
            "user_id",
            "message_id",
        ),
        "images.csv": _schema(
            "images.csv",
            (_id("image_id"), _text("file_path", path=True)),
            "image_id",
        ),
        "voice_notes.csv": _schema(
            "voice_notes.csv",
            (_id("voice_note_id"), _text("file_path", path=True)),
            "voice_note_id",
        ),
        "daily_notification_summary.csv": _schema(
            "daily_notification_summary.csv",
            (
                _id("user_id"),
                _date("date"),
                _int("notifications_sent"),
                _int("notifications_dismissed"),
            ),
            "user_id",
            "date",
        ),
        "sample_messages.csv": _schema(
            "sample_messages.csv",
            MESSAGE_FIELDS + OUTPUT_FIELDS[1:],
            "message_id",
        ),
        "output.csv": _schema("output.csv", OUTPUT_FIELDS, "message_id"),
    }
)


REQUIRED_RUNTIME_FILES = (
    "messages.csv",
    "users.csv",
    "groups.csv",
    "group_members.csv",
    "business_accounts.csv",
    "user_business_history.csv",
    "message_history.csv",
    "message_events.csv",
    "images.csv",
    "voice_notes.csv",
    "daily_notification_summary.csv",
)


def schema_for(table_name: str | Path) -> TableSchema:
    filename = Path(table_name).name
    try:
        return TABLE_SCHEMAS[filename]
    except KeyError as exc:
        raise KeyError(f"no schema is registered for {filename!r}") from exc


def _header_issue(table_name: str, header: list[str], expected: tuple[str, ...]) -> ValidationIssue:
    missing = [column for column in expected if column not in header]
    extra = [column for column in header if column not in expected]
    if missing:
        code = "SCHEMA_MISSING_COLUMN"
        detail = "missing=" + ",".join(missing)
    elif extra:
        code = "SCHEMA_EXTRA_COLUMN"
        detail = "extra=" + ",".join(extra)
    else:
        code = "SCHEMA_COLUMN_ORDER_INVALID"
        detail = "columns must match the declared order exactly"
    return ValidationIssue(code=code, table=table_name, detail=detail)


def _validate_key_uniqueness(
    table_name: str,
    rows: list[Mapping[str, object]],
    primary_key: tuple[str, ...],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    seen: dict[tuple[object, ...], int] = {}
    for row_number, row in enumerate(rows, start=2):
        key = tuple(row[column] for column in primary_key)
        if key in seen:
            issues.append(
                ValidationIssue(
                    code="DUPLICATE_KEY",
                    table=table_name,
                    row_number=row_number,
                    detail="duplicate primary key; first seen at row " + str(seen[key]),
                )
            )
        else:
            seen[key] = row_number
    return issues


def load_csv_table(path: str | Path, table_name: str | None = None) -> tuple[Mapping[str, object], ...]:
    """Load one registered CSV with exact ordered columns and typed values."""

    csv_path = Path(path)
    schema = schema_for(table_name or csv_path.name)
    table = schema.filename
    issues: list[ValidationIssue] = []
    if not csv_path.is_file():
        raise DatasetValidationError(
            [ValidationIssue("INPUT_MISSING", table=table, detail="file does not exist")]
        )

    parsed_rows: list[Mapping[str, object]] = []
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, strict=True)
            try:
                header = next(reader)
            except StopIteration:
                raise DatasetValidationError(
                    [ValidationIssue("SCHEMA_MISSING_COLUMN", table=table, detail="empty CSV")]
                )
            if header != list(schema.columns):
                raise DatasetValidationError([_header_issue(table, header, schema.columns)])

            for row_number, raw_row in enumerate(reader, start=2):
                if len(raw_row) != len(schema.fields):
                    issues.append(
                        ValidationIssue(
                            "CSV_PARSE_FAILED",
                            table=table,
                            row_number=row_number,
                            detail=(
                                f"expected {len(schema.fields)} fields, got {len(raw_row)}"
                            ),
                        )
                    )
                    continue
                parsed: dict[str, object] = {}
                for field, raw_value in zip(schema.fields, raw_row):
                    try:
                        parsed[field.name] = field.parse(raw_value)
                    except EnumValueError as exc:
                        issues.append(
                            ValidationIssue(
                                "ENUM_INVALID",
                                table=table,
                                row_number=row_number,
                                field=field.name,
                                detail=str(exc),
                            )
                        )
                    except ValueError as exc:
                        issues.append(
                            ValidationIssue(
                                "TYPE_INVALID",
                                table=table,
                                row_number=row_number,
                                field=field.name,
                                detail=str(exc),
                            )
                        )
                if len(parsed) == len(schema.fields):
                    parsed_rows.append(MappingProxyType(parsed))
    except DatasetValidationError:
        raise
    except (OSError, UnicodeError, csv.Error) as exc:
        raise DatasetValidationError(
            [ValidationIssue("CSV_PARSE_FAILED", table=table, detail=type(exc).__name__)]
        ) from exc

    issues.extend(_validate_key_uniqueness(table, parsed_rows, schema.primary_key))
    if issues:
        raise DatasetValidationError(issues)
    return tuple(parsed_rows)


def load_csv(path: str | Path, table_name: str | None = None) -> tuple[Mapping[str, object], ...]:
    """Backward-friendly alias for the exact-schema table loader."""

    return load_csv_table(path, table_name)

"""Label-free inputs that are safe to pass to routing code."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping


INPUT_COLUMNS = (
    "message_id",
    "user_id",
    "conversation_type",
    "group_id",
    "business_id",
    "sender_user_id",
    "created_at",
    "message_text",
    "media_type",
    "media_id",
    "forwarded_count",
)


@dataclass(frozen=True, slots=True)
class SanitizedMessage:
    """Exactly the 11 participant input columns, with no sample labels."""

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

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> "SanitizedMessage":
        """Copy only the allowlisted input fields from a typed CSV row."""

        missing = [column for column in INPUT_COLUMNS if column not in row]
        if missing:
            raise ValueError("sanitized row is missing input columns")
        return cls(
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

    def as_dict(self) -> dict[str, object]:
        """Serialize only the exact label-free router input schema."""

        return {
            "message_id": self.message_id,
            "user_id": self.user_id,
            "conversation_type": self.conversation_type,
            "group_id": self.group_id,
            "business_id": self.business_id,
            "sender_user_id": self.sender_user_id,
            "created_at": self.created_at.isoformat(sep=" ", timespec="minutes"),
            "message_text": self.message_text,
            "media_type": self.media_type,
            "media_id": self.media_id,
            "forwarded_count": self.forwarded_count,
        }

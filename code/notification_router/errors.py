"""Stable, redacted validation errors used by the Milestone 1 harness."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One bounded validation finding.

    The issue intentionally stores locations and short diagnostics, never full
    message contents or media bytes.
    """

    code: str
    table: str
    row_number: int | None = None
    field: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "table": self.table,
            "row_number": self.row_number,
            "field": self.field,
            "detail": self.detail,
        }

    def format(self) -> str:
        location = self.table
        if self.row_number is not None:
            location += f" row {self.row_number}"
        if self.field:
            location += f" field {self.field}"
        suffix = f": {self.detail}" if self.detail else ""
        return f"{self.code} ({location}){suffix}"


class DatasetValidationError(ValueError):
    """Raised when a dataset cannot safely enter the normalized pipeline."""

    def __init__(self, issues: list[ValidationIssue] | tuple[ValidationIssue, ...]):
        self.issues = tuple(issues)
        message = "\n".join(issue.format() for issue in self.issues)
        super().__init__(message or "dataset validation failed")


def raise_if_issues(issues: list[ValidationIssue]) -> None:
    """Raise one deterministic aggregate error when ``issues`` is non-empty."""

    if issues:
        raise DatasetValidationError(issues)

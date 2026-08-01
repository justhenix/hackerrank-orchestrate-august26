"""Diagnostic CLI implementation for Architecture v0.1 Milestone 1.

This entry point validates and joins participant data, checks strictly-prior
history, and sniffs media bytes. It intentionally produces no predictions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .dataset import load_dataset, normalize_dataset
from .errors import DatasetValidationError
from .media import media_summary, sniff_dataset_media


def _diagnostic_report(dataset_dir: Path) -> dict[str, object]:
    tables = load_dataset(dataset_dir)
    normalized = normalize_dataset(tables)
    sniffed = sniff_dataset_media(tables)

    all_same_user = sum(
        historical.user_id == incoming.user_id
        for incoming in normalized.messages
        for historical in normalized.history
    )
    prior_rows = sum(
        len(normalized.strictly_prior_history(incoming)) for incoming in normalized.messages
    )
    coverage = normalized.join_coverage
    return {
        "architecture": "Architecture v0.1",
        "milestone": "M1",
        "dataset_root": str(tables.dataset_root),
        "timestamp_policy": tables.timestamp_policy,
        "tables": {
            "messages.csv": len(tables.messages),
            "users.csv": len(tables.users),
            "groups.csv": len(tables.groups),
            "group_members.csv": len(tables.group_members),
            "business_accounts.csv": len(tables.business_accounts),
            "user_business_history.csv": len(tables.user_business_history),
            "message_history.csv": len(tables.message_history),
            "message_events.csv": len(tables.message_events),
            "images.csv": len(tables.images),
            "voice_notes.csv": len(tables.voice_notes),
            "daily_notification_summary.csv": len(tables.daily_notification_summary),
        },
        "integrity": "passed",
        "normalized_joins": {
            "messages": coverage.total_messages,
            "users_joined": coverage.users_joined,
            "groups_joined": coverage.groups_joined,
            "group_memberships_joined": coverage.group_memberships_joined,
            "sender_users_joined": coverage.sender_users_joined,
            "businesses_joined": coverage.businesses_joined,
            "business_histories_joined": coverage.business_histories_joined,
            "daily_summaries_joined": coverage.daily_summaries_joined,
            "optional_missing": dict(sorted(coverage.optional_missing.items())),
        },
        "temporal_filter": {
            "same_user_pairs": all_same_user,
            "strictly_prior_pairs": prior_rows,
            "excluded_same_time_or_future_pairs": all_same_user - prior_rows,
            "target_rows_added_to_history": 0,
        },
        "media": media_summary(sniffed),
        "predictions_written": False,
    }


def _print_human(report: dict[str, object]) -> None:
    print(f"{report['architecture']} / {report['milestone']} diagnostics")
    print(f"dataset: {report['dataset_root']}")
    print(f"timestamp policy: {report['timestamp_policy']}")
    print("integrity: passed")
    print("tables:")
    for filename, count in report["tables"].items():
        print(f"  {filename}: {count}")
    joins = report["normalized_joins"]
    print(
        "normalized joins: "
        f"{joins['messages']} messages, {joins['users_joined']} users, "
        f"{joins['groups_joined']} groups"
    )
    print(f"optional missing: {joins['optional_missing']}")
    temporal = report["temporal_filter"]
    print(
        "strictly-prior history: "
        f"{temporal['strictly_prior_pairs']} of {temporal['same_user_pairs']} same-user pairs; "
        f"excluded={temporal['excluded_same_time_or_future_pairs']}"
    )
    media = report["media"]
    print(
        "media sniffing: "
        f"{media['records']} records, states={media['signature_states']}, "
        f"formats={media['detected_formats']}"
    )
    print(f"extension mismatches: {media['extension_mismatches']}")
    print("predictions written: false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate, normalize, temporally filter, and sniff the dataset; write no predictions."
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=Path("dataset"),
        help="participant dataset directory (default: dataset)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one deterministic JSON diagnostic object",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = _diagnostic_report(args.dataset_dir)
    except DatasetValidationError as exc:
        print(f"validation failed: {len(exc.issues)} issue(s)", file=sys.stderr)
        for issue in exc.issues:
            print(f"- {issue.format()}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"diagnostic failed: {type(exc).__name__}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        _print_human(report)
    return 0


if __name__ == "__main__":  # pragma: no cover - covered by CLI smoke tests.
    raise SystemExit(main())

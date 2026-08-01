from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from notification_router.dataset import (
    load_dataset,
    normalize_dataset,
    validate_key_integrity,
    validate_referential_integrity,
)
from notification_router.errors import DatasetValidationError
from notification_router.media import media_summary, sniff_bytes, sniff_dataset_media, sniff_media_file
from notification_router.schemas import schema_for, load_csv_table


SUBMISSION_ROOT = Path(__file__).resolve().parents[1]
ROOT = SUBMISSION_ROOT.parent
DATASET = ROOT / "dataset"


def write_csv(path: Path, columns: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)


class MilestoneOneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tables = load_dataset(DATASET)
        cls.normalized = normalize_dataset(cls.tables)

    def test_real_dataset_loads_with_typed_rows_and_joins(self) -> None:
        self.assertEqual(len(self.tables.messages), 110)
        self.assertEqual(len(self.tables.message_history), 412)
        self.assertIsNone(self.tables.messages[0].created_at.tzinfo)
        self.assertEqual(
            self.tables.timestamp_policy, "dataset-local-naive-wall-clock"
        )
        self.assertEqual(self.normalized.join_coverage.users_joined, 110)
        self.assertEqual(self.normalized.join_coverage.groups_joined, 63)
        self.assertEqual(self.normalized.join_coverage.businesses_joined, 30)

        group_message = next(
            message for message in self.tables.messages if message.conversation_type == "group"
        )
        context = self.normalized.context_for(group_message.message_id)
        self.assertIsNotNone(context.group)
        self.assertIsNotNone(context.group_member)
        self.assertIsNotNone(context.sender)

    def test_exact_schema_rejects_missing_extra_and_reordered_columns(self) -> None:
        columns = list(schema_for("users.csv").columns)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.csv"
            write_csv(missing, columns[:-1], [])
            with self.assertRaises(DatasetValidationError) as missing_error:
                load_csv_table(missing, "users.csv")
            self.assertEqual(missing_error.exception.issues[0].code, "SCHEMA_MISSING_COLUMN")

            extra = root / "extra.csv"
            write_csv(extra, columns + ["unexpected"], [])
            with self.assertRaises(DatasetValidationError) as extra_error:
                load_csv_table(extra, "users.csv")
            self.assertEqual(extra_error.exception.issues[0].code, "SCHEMA_EXTRA_COLUMN")

            reordered = root / "reordered.csv"
            write_csv(reordered, columns[1:] + columns[:1], [])
            with self.assertRaises(DatasetValidationError) as order_error:
                load_csv_table(reordered, "users.csv")
            self.assertEqual(
                order_error.exception.issues[0].code, "SCHEMA_COLUMN_ORDER_INVALID"
            )

    def test_schema_types_enums_and_composite_keys_are_checked(self) -> None:
        message_columns = list(schema_for("messages.csv").columns)
        invalid_message = [
            "msg_test",
            "u_test",
            "not_a_conversation",
            "",
            "",
            "u_sender",
            "2026-08-01 10:00",
            "hello",
            "",
            "",
            "0",
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "invalid.csv"
            write_csv(invalid, message_columns, [invalid_message])
            with self.assertRaises(DatasetValidationError) as enum_error:
                load_csv_table(invalid, "messages.csv")
            self.assertEqual(enum_error.exception.issues[0].code, "ENUM_INVALID")

            user_columns = list(schema_for("users.csv").columns)
            user_row = ["u_1", "22:00-07:00", "1", "1", "0", "0"]
            duplicate = root / "duplicate.csv"
            write_csv(duplicate, user_columns, [user_row, user_row])
            with self.assertRaises(DatasetValidationError) as duplicate_error:
                load_csv_table(duplicate, "users.csv")
            self.assertTrue(
                any(issue.code == "DUPLICATE_KEY" for issue in duplicate_error.exception.issues)
            )

    def test_voice_rows_allow_empty_message_text(self) -> None:
        voice_message = next(message for message in self.tables.messages if message.media_type == "voice")
        self.assertEqual(voice_message.message_text, "")
        self.assertIsNotNone(voice_message.media_id)

    def test_referential_integrity_rejects_unknown_user_and_unsafe_media_path(self) -> None:
        broken_message = replace(self.tables.messages[0], user_id="user_that_does_not_exist")
        broken_tables = replace(self.tables, messages=(broken_message,) + self.tables.messages[1:])
        issues = validate_referential_integrity(broken_tables)
        self.assertTrue(any(issue.code == "REFERENCE_BROKEN" for issue in issues))

        unsafe_image = replace(self.tables.images[0], file_path="../outside.jpg")
        unsafe_tables = replace(self.tables, images=(unsafe_image,) + self.tables.images[1:])
        unsafe_issues = validate_referential_integrity(unsafe_tables)
        self.assertTrue(
            any(issue.field == "file_path" and issue.code == "REFERENCE_BROKEN" for issue in unsafe_issues)
        )

    def test_target_and_history_ids_cannot_overlap(self) -> None:
        duplicate_history = replace(
            self.tables.message_history[0], message_id=self.tables.messages[0].message_id
        )
        broken_tables = replace(
            self.tables,
            message_history=(duplicate_history,) + self.tables.message_history[1:],
        )
        issues = validate_key_integrity(broken_tables)
        self.assertTrue(any(issue.code == "DUPLICATE_KEY" for issue in issues))

    def test_strict_temporal_filter_excludes_same_time_future_and_cross_user(self) -> None:
        target = self.tables.messages[0]
        base = next(row for row in self.tables.message_history if row.user_id == target.user_id)
        other_user = next(user.user_id for user in self.tables.users if user.user_id != target.user_id)
        earlier = replace(
            base, message_id="synthetic_earlier", created_at=target.created_at - timedelta(minutes=1)
        )
        same_time = replace(base, message_id="synthetic_same_time", created_at=target.created_at)
        future = replace(
            base, message_id="synthetic_future", created_at=target.created_at + timedelta(minutes=1)
        )
        cross_user = replace(base, message_id="synthetic_cross_user", user_id=other_user)
        modified = replace(
            self.tables,
            message_history=self.tables.message_history + (earlier, same_time, future, cross_user),
        )
        normalized = normalize_dataset(modified)
        prior_ids = {
            row.message_id for row in normalized.strictly_prior_history_for(target.message_id)
        }
        self.assertIn("synthetic_earlier", prior_ids)
        self.assertNotIn("synthetic_same_time", prior_ids)
        self.assertNotIn("synthetic_future", prior_ids)
        self.assertNotIn("synthetic_cross_user", prior_ids)

    def test_media_sniffing_uses_bytes_not_extensions(self) -> None:
        signatures = {
            "jpeg": b"\xff\xd8\xff\xe0",
            "png": b"\x89PNG\r\n\x1a\n",
            "webp": b"RIFF\x00\x00\x00\x00WEBP",
            "avif": b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00",
            "mp3": b"ID3\x04\x00\x00",
            "m4a": b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00",
            "wav": b"RIFF\x00\x00\x00\x00WAVE",
        }
        for expected, data in signatures.items():
            self.assertEqual(sniff_bytes(data), expected)
        self.assertEqual(sniff_bytes(b"not a supported media signature"), "unknown")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            disguised = root / "media" / "image.jpg"
            disguised.parent.mkdir()
            disguised.write_bytes(signatures["webp"])
            result = sniff_media_file(
                root,
                media_id="synthetic_media",
                declared_media_type="image",
                declared_path="media/image.jpg",
            )
            self.assertEqual(result.detected_format, "webp")
            self.assertEqual(result.extension_format, "jpeg")
            self.assertTrue(result.format_matches_media_type)

    def test_media_catalog_is_recognized_and_reports_extension_mismatches(self) -> None:
        results = sniff_dataset_media(self.tables)
        summary = media_summary(results)
        self.assertEqual(summary["records"], 33)
        self.assertEqual(summary["signature_states"], {"recognized": 33})
        self.assertGreater(summary["extension_mismatches"], 0)
        self.assertEqual(summary["declaration_mismatches"], 0)

    def test_diagnostic_cli_is_read_only_and_json_serializable(self) -> None:
        output_path = DATASET / "output.csv"
        before_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
        completed = subprocess.run(
            [
                sys.executable,
                "main.py",
                "--dataset-dir",
                str(DATASET),
                "--json",
            ],
            cwd=SUBMISSION_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        after_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
        self.assertEqual(before_hash, after_hash)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["integrity"], "passed")
        self.assertEqual(
            report["timestamp_policy"], "dataset-local-naive-wall-clock"
        )
        self.assertFalse(report["predictions_written"])


if __name__ == "__main__":
    unittest.main()

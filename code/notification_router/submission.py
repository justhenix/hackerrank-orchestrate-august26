"""Target-output validation and atomic CSV writing for Architecture v0.1."""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .artifacts import sha256_file
from .dataset import load_csv_table
from .predictions import RawPrediction, prediction_from_mapping, prediction_schema_errors
from .retrieval import EvidenceProvenanceError, validate_selected_evidence
from .schemas import OUTPUT_FIELDS


OUTPUT_COLUMNS = tuple(field.name for field in OUTPUT_FIELDS)


class SubmissionValidationError(ValueError):
    """Raised when target predictions cannot satisfy the output contract."""

    def __init__(self, errors: Sequence[str]):
        self.errors = tuple(str(error) for error in errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True, slots=True)
class SubmissionArtifact:
    """Safe metadata for a validated output artifact."""

    path: Path
    row_count: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "filename": self.path.name,
            "row_count": self.row_count,
            "sha256": self.sha256,
        }


def _prediction_errors(
    predictions: Sequence[RawPrediction],
    expected_ids: Sequence[str],
    evidence_allowlists: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    expected = tuple(expected_ids)
    expected_set = set(expected)
    errors: list[str] = []
    if len(expected_set) != len(expected):
        errors.append("expected message IDs are not unique")

    observed = tuple(prediction.message_id for prediction in predictions)
    observed_set = set(observed)
    if len(observed_set) != len(observed):
        errors.append("prediction message IDs are not unique")
    if len(predictions) != len(expected):
        errors.append(f"row count mismatch: expected {len(expected)}, got {len(predictions)}")
    missing = expected_set - observed_set
    extra = observed_set - expected_set
    if missing:
        errors.append(f"missing message IDs: {len(missing)}")
    if extra:
        errors.append(f"unexpected message IDs: {len(extra)}")

    for index, prediction in enumerate(predictions):
        fields = prediction_schema_errors(prediction)
        if fields:
            errors.append(f"row {index} schema fields: {','.join(fields)}")
        if prediction.message_id not in expected_set:
            continue
        allowlist = tuple(evidence_allowlists.get(prediction.message_id, ()))
        if prediction.message_id not in evidence_allowlists:
            errors.append(f"row {index} evidence allowlist missing")
            continue
        try:
            validate_selected_evidence(
                prediction.selected_evidence_message_ids,
                allowlist,
            )
        except EvidenceProvenanceError:
            errors.append(f"row {index} evidence is not allowlisted")
    return tuple(errors)


def validate_predictions(
    predictions: Sequence[RawPrediction],
    *,
    expected_ids: Sequence[str],
    evidence_allowlists: Mapping[str, tuple[str, ...]],
) -> tuple[RawPrediction, ...]:
    """Validate exact target IDs, final fields, enums, confidence, and evidence."""

    errors = _prediction_errors(predictions, expected_ids, evidence_allowlists)
    if errors:
        raise SubmissionValidationError(errors)
    return tuple(predictions)


def _evidence_text(ids: Sequence[str]) -> str:
    return "none" if not ids else ";".join(ids)


def write_output_csv(
    path: str | Path,
    predictions: Sequence[RawPrediction],
    *,
    expected_ids: Sequence[str],
    evidence_allowlists: Mapping[str, tuple[str, ...]],
) -> SubmissionArtifact:
    """Validate and atomically replace the requested output CSV."""

    checked = validate_predictions(
        predictions,
        expected_ids=expected_ids,
        evidence_allowlists=evidence_allowlists,
    )
    output_path = Path(path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    if temporary.exists():
        raise SubmissionValidationError(("temporary output path already exists",))
    try:
        with temporary.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, lineterminator="\n")
            writer.writeheader()
            for prediction in checked:
                writer.writerow(
                    {
                        "message_id": prediction.message_id,
                        "action": prediction.action,
                        "message_type": prediction.message_type,
                        "reason": prediction.reason,
                        "confidence": format(float(prediction.confidence), ".12g"),
                        "evidence_message_ids": _evidence_text(
                            prediction.selected_evidence_message_ids
                        ),
                    }
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output_path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    return SubmissionArtifact(
        path=output_path,
        row_count=len(checked),
        sha256=sha256_file(output_path),
    )


def validate_output_csv(
    path: str | Path,
    *,
    expected_ids: Sequence[str],
    evidence_allowlists: Mapping[str, tuple[str, ...]],
) -> SubmissionArtifact:
    """Reparse an output CSV and reapply the exact submission contract."""

    output_path = Path(path).resolve()
    rows = load_csv_table(output_path, "output.csv")
    predictions: list[RawPrediction] = []
    for row in rows:
        raw_evidence = str(row["evidence_message_ids"])
        selected = () if raw_evidence == "none" else tuple(raw_evidence.split(";"))
        predictions.append(
            prediction_from_mapping(
                {
                    "message_id": row["message_id"],
                    "action": row["action"],
                    "message_type": row["message_type"],
                    "reason": row["reason"],
                    "confidence": float(row["confidence"]),
                    "selected_evidence_message_ids": selected,
                }
            )
        )
    validate_predictions(
        predictions,
        expected_ids=expected_ids,
        evidence_allowlists=evidence_allowlists,
    )
    return SubmissionArtifact(
        path=output_path,
        row_count=len(rows),
        sha256=sha256_file(output_path),
    )

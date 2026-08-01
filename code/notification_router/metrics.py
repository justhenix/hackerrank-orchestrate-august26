"""Deterministic schema, decision, evidence, confidence, latency, and cost metrics."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
from typing import Mapping, Sequence

from .predictions import RawPrediction, prediction_schema_errors
from .retrieval import EvidenceProvenanceError, validate_selected_evidence
from .schemas import ACTION_VALUES, MESSAGE_TYPE_VALUES


ACTION_ORDER = ("notify", "digest", "mute")
TYPE_ORDER = (
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
)


@dataclass(frozen=True, slots=True)
class ExpectedDecision:
    """Evaluator-only expected fields; never included in router inputs."""

    message_id: str
    action: str
    message_type: str
    evidence_message_ids: tuple[str, ...]


def _safe_rate(numerator: int | float, denominator: int | float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def _confusion(
    expected: Sequence[tuple[str, str]],
    predicted: Mapping[str, str | None],
    labels: Sequence[str],
) -> dict[str, dict[str, int]]:
    matrix = {label: {other: 0 for other in labels} for label in labels}
    for expected_value, message_id in expected:
        predicted_value = predicted.get(message_id)
        if expected_value in matrix and predicted_value in matrix[expected_value]:
            matrix[expected_value][predicted_value] += 1
    return matrix


def _class_scores(
    expected: Sequence[str], predicted: Mapping[str, str | None], ids: Sequence[str], labels: Sequence[str]
) -> tuple[dict[str, dict[str, float | int]], float]:
    rows: dict[str, dict[str, float | int]] = {}
    f1_values: list[float] = []
    for label in labels:
        true_positive = sum(
            expected_value == label and predicted.get(message_id) == label
            for message_id, expected_value in zip(ids, expected)
        )
        false_positive = sum(
            expected_value != label and predicted.get(message_id) == label
            for message_id, expected_value in zip(ids, expected)
        )
        false_negative = sum(
            expected_value == label and predicted.get(message_id) != label
            for message_id, expected_value in zip(ids, expected)
        )
        precision = _safe_rate(true_positive, true_positive + false_positive)
        recall = _safe_rate(true_positive, true_positive + false_negative)
        f1 = _safe_rate(2 * precision * recall, precision + recall)
        support = sum(value == label for value in expected)
        rows[label] = {
            "support": support,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
        f1_values.append(f1)
    return rows, _safe_rate(sum(f1_values), len(f1_values))


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None}
    return {
        "count": len(values),
        "mean": sum(values) / len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
    }


def _confidence_metrics(
    expected_by_id: Mapping[str, ExpectedDecision],
    predictions_by_id: Mapping[str, RawPrediction],
) -> dict[str, object]:
    rows = [
        (expected_by_id[message_id], predictions_by_id[message_id])
        for message_id in expected_by_id
        if message_id in predictions_by_id
        and prediction_schema_errors(predictions_by_id[message_id]) == ()
    ]
    confidences = [float(prediction.confidence) for _, prediction in rows]
    if not confidences:
        return {
            "raw_confidence_count": 0,
            "raw_confidence_mean": None,
            "correctness_brier": None,
            "expected_calibration_error": None,
            "bins": 10,
        }
    correctness = [
        float(prediction.action == expected.action) for expected, prediction in rows
    ]
    brier = sum(
        (confidence - correct) ** 2 for confidence, correct in zip(confidences, correctness)
    ) / len(confidences)
    bins: list[list[int]] = [[] for _ in range(10)]
    for index, confidence in enumerate(confidences):
        bin_index = min(int(confidence * 10), 9)
        bins[bin_index].append(index)
    ece = 0.0
    for bin_rows in bins:
        if not bin_rows:
            continue
        average_confidence = sum(confidences[index] for index in bin_rows) / len(bin_rows)
        average_accuracy = sum(correctness[index] for index in bin_rows) / len(bin_rows)
        ece += _safe_rate(len(bin_rows), len(confidences)) * abs(
            average_confidence - average_accuracy
        )
    return {
        "raw_confidence_count": len(confidences),
        "raw_confidence_mean": sum(confidences) / len(confidences),
        "correctness_brier": brier,
        "expected_calibration_error": ece,
        "bins": 10,
    }


def _evidence_metrics(
    expected: Sequence[ExpectedDecision],
    predictions_by_id: Mapping[str, RawPrediction],
    allowlists: Mapping[str, tuple[str, ...]],
) -> dict[str, object]:
    valid_rows = 0
    selected_count = 0
    valid_selected_count = 0
    true_positive = 0
    false_positive = 0
    false_negative = 0
    expected_nonempty = 0
    covered_nonempty = 0
    for expected_row in expected:
        prediction = predictions_by_id.get(expected_row.message_id)
        if prediction is None:
            continue
        allowlist = tuple(allowlists.get(expected_row.message_id, ()))
        selected = tuple(prediction.selected_evidence_message_ids)
        selected_count += len(selected)
        try:
            validate_selected_evidence(selected, allowlist)
            valid_rows += 1
            valid_selected_count += len(selected)
        except EvidenceProvenanceError:
            pass
        expected_ids = set(expected_row.evidence_message_ids)
        selected_ids = set(selected)
        true_positive += len(expected_ids & selected_ids)
        false_positive += len(selected_ids - expected_ids)
        false_negative += len(expected_ids - selected_ids)
        if expected_ids:
            expected_nonempty += 1
            if selected_ids & set(allowlist):
                covered_nonempty += 1
    precision = _safe_rate(true_positive, true_positive + false_positive)
    recall = _safe_rate(true_positive, true_positive + false_negative)
    return {
        "allowlist_provenance_valid_rate": _safe_rate(valid_rows, len(expected)),
        "valid_selected_id_rate": _safe_rate(valid_selected_count, selected_count),
        "selected_id_count": selected_count,
        "exact_set_precision": precision,
        "exact_set_recall": recall,
        "exact_set_f1": _safe_rate(2 * precision * recall, precision + recall),
        "evidence_coverage": _safe_rate(covered_nonempty, expected_nonempty),
    }


def compute_metrics(
    expected: Sequence[ExpectedDecision],
    predictions: Sequence[RawPrediction],
    allowlists: Mapping[str, tuple[str, ...]] | None = None,
) -> dict[str, object]:
    """Compute deterministic development metrics over raw proposals."""

    allowlists = allowlists or {}
    expected_ids = tuple(row.message_id for row in expected)
    expected_by_id = {row.message_id: row for row in expected}
    prediction_counts = Counter(prediction.message_id for prediction in predictions)
    predictions_by_id: dict[str, RawPrediction] = {}
    for prediction in predictions:
        predictions_by_id.setdefault(prediction.message_id, prediction)
    observed_ids = set(predictions_by_id)
    expected_id_set = set(expected_ids)
    duplicate_rows = sum(max(count - 1, 0) for count in prediction_counts.values())
    missing_ids = sorted(expected_id_set - observed_ids)
    extra_ids = sorted(observed_ids - expected_id_set)
    schema_valid_rows = sum(
        prediction.message_id in expected_id_set
        and prediction_counts[prediction.message_id] == 1
        and not prediction_schema_errors(prediction)
        for prediction in predictions
    )
    schema_denominator = max(len(expected), len(predictions), 1)

    action_predictions = {
        message_id: prediction.action for message_id, prediction in predictions_by_id.items()
    }
    type_predictions = {
        message_id: prediction.message_type for message_id, prediction in predictions_by_id.items()
    }
    valid_action_rows = [
        row for row in expected if row.message_id in predictions_by_id
    ]
    action_accuracy = _safe_rate(
        sum(predictions_by_id[row.message_id].action == row.action for row in valid_action_rows),
        len(valid_action_rows),
    )
    type_accuracy = _safe_rate(
        sum(
            predictions_by_id[row.message_id].message_type == row.message_type
            for row in valid_action_rows
        ),
        len(valid_action_rows),
    )
    action_expected = [row.action for row in valid_action_rows]
    action_ids = [row.message_id for row in valid_action_rows]
    type_expected = [row.message_type for row in valid_action_rows]
    action_scores, action_macro_f1 = _class_scores(
        action_expected, action_predictions, action_ids, ACTION_ORDER
    )
    type_scores, type_macro_f1 = _class_scores(
        type_expected, type_predictions, action_ids, TYPE_ORDER
    )
    joint_exact = sum(
        predictions_by_id[row.message_id].action == row.action
        and predictions_by_id[row.message_id].message_type == row.message_type
        for row in valid_action_rows
    )
    latency_values = [
        float(prediction.latency_ms)
        for prediction in predictions
        if isinstance(prediction.latency_ms, (int, float))
        and not isinstance(prediction.latency_ms, bool)
        and math.isfinite(float(prediction.latency_ms))
        and float(prediction.latency_ms) >= 0
    ]
    cost_values = [
        float(prediction.cost_usd)
        for prediction in predictions
        if isinstance(prediction.cost_usd, (int, float))
        and not isinstance(prediction.cost_usd, bool)
        and math.isfinite(float(prediction.cost_usd))
        and float(prediction.cost_usd) >= 0
    ]
    error_counts = Counter(
        prediction.error_code for prediction in predictions if prediction.error_code
    )
    invalid_field_counts = Counter(
        field
        for prediction in predictions
        for field in prediction_schema_errors(prediction)
    )
    return {
        "schema": {
            "expected_rows": len(expected),
            "observed_rows": len(predictions),
            "missing_rows": len(missing_ids),
            "extra_rows": len(extra_ids),
            "duplicate_rows": duplicate_rows,
            "missing_message_ids": missing_ids,
            "extra_message_ids": extra_ids,
            "invalid_fields": dict(sorted(invalid_field_counts.items())),
            "schema_valid_rate": _safe_rate(schema_valid_rows, schema_denominator),
        },
        "action": {
            "accuracy": action_accuracy,
            "macro_f1": action_macro_f1,
            "confusion": _confusion(
                [(row.action, row.message_id) for row in valid_action_rows],
                action_predictions,
                ACTION_ORDER,
            ),
            "per_class": action_scores,
        },
        "message_type": {
            "accuracy": type_accuracy,
            "macro_f1": type_macro_f1,
            "per_class": type_scores,
        },
        "joint": {"action_type_exact_match": _safe_rate(joint_exact, len(valid_action_rows))},
        "evidence": _evidence_metrics(expected, predictions_by_id, allowlists),
        "confidence": _confidence_metrics(expected_by_id, predictions_by_id),
        "operations": {
            "latency_ms": _distribution(latency_values),
            "cost_usd": {
                **_distribution(cost_values),
                "total": sum(cost_values) if cost_values else 0.0,
            },
            "error_counts": dict(sorted(error_counts.items())),
        },
    }

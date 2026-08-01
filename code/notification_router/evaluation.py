"""Label-isolated development/holdout evaluation harness."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .artifacts import (
    ImmutableArtifactStore,
    RunManifest,
    build_run_manifest,
    canonical_hash,
    sha256_file,
)
from .inputs import SanitizedMessage
from .metrics import ExpectedDecision, compute_metrics
from .predictions import RawPrediction
from .schemas import ACTION_VALUES, load_csv_table


SPLIT_SALT = "architecture-draft-v0.1-split|"
SPLIT_ALGORITHM = "sha256-sort-stratified-v1"
EXPECTED_SAMPLE_ROWS = 30
HOLDOUT_COUNTS = {"notify": 3, "digest": 4, "mute": 3}
DEVELOPMENT_COUNTS = {"notify": 6, "digest": 7, "mute": 7}
PARTITIONS = ("development", "holdout")


class EvaluationHarnessError(ValueError):
    """Raised for deterministic split, partition, or isolation violations."""


class HoldoutSealedError(EvaluationHarnessError):
    """Raised when sealed rows are requested without an explicit reveal."""


@dataclass(frozen=True, slots=True)
class LabeledSample:
    """Evaluator-only row; this type must never be handed to routing code."""

    input: SanitizedMessage
    expected: ExpectedDecision
    expected_reason: str
    expected_confidence: float
    source_index: int


@dataclass(frozen=True, slots=True)
class SplitPlan:
    """Full evaluator-side assignment, including sealed IDs."""

    rows: tuple[LabeledSample, ...]
    development_rows: tuple[LabeledSample, ...]
    holdout_rows: tuple[LabeledSample, ...]
    source_file_sha256: str
    manifest_payload: Mapping[str, object]
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    manifest: RunManifest
    metrics: Mapping[str, object]
    artifact_directory: Path


def _parse_evidence(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise EvaluationHarnessError("sample evidence field must be text")
    if value == "none":
        return ()
    ids = tuple(part.strip() for part in value.split(";") if part.strip())
    if len(set(ids)) != len(ids):
        raise EvaluationHarnessError("sample evidence contains duplicate IDs")
    return ids


def _load_labeled_samples(sample_path: Path) -> tuple[LabeledSample, ...]:
    rows = load_csv_table(sample_path, "sample_messages.csv")
    if len(rows) != EXPECTED_SAMPLE_ROWS:
        raise EvaluationHarnessError(
            f"expected {EXPECTED_SAMPLE_ROWS} sample rows, got {len(rows)}"
        )
    samples: list[LabeledSample] = []
    for source_index, row in enumerate(rows):
        action = row["action"]
        if action not in ACTION_VALUES:
            raise EvaluationHarnessError("sample action is outside the contract domain")
        sanitized = SanitizedMessage.from_row(row)
        expected = ExpectedDecision(
            message_id=sanitized.message_id,
            action=action,
            message_type=row["message_type"],
            evidence_message_ids=_parse_evidence(row["evidence_message_ids"]),
        )
        samples.append(
            LabeledSample(
                input=sanitized,
                expected=expected,
                expected_reason=row["reason"],
                expected_confidence=float(row["confidence"]),
                source_index=source_index,
            )
        )
    return tuple(samples)


def _split_rows(rows: tuple[LabeledSample, ...], source_hash: str) -> SplitPlan:
    action_groups: dict[str, list[LabeledSample]] = {action: [] for action in ACTION_VALUES}
    for row in rows:
        action_groups[row.expected.action].append(row)
    holdout_ids: set[str] = set()
    for action, group in action_groups.items():
        if len(group) != HOLDOUT_COUNTS[action] + DEVELOPMENT_COUNTS[action]:
            raise EvaluationHarnessError(f"unexpected {action} sample count")
        ranked = sorted(
            group,
            key=lambda row: (
                hashlib.sha256((SPLIT_SALT + row.input.message_id).encode("utf-8")).hexdigest(),
                row.input.message_id,
            ),
        )
        holdout_ids.update(row.input.message_id for row in ranked[: HOLDOUT_COUNTS[action]])
    development_rows = tuple(row for row in rows if row.input.message_id not in holdout_ids)
    holdout_rows = tuple(row for row in rows if row.input.message_id in holdout_ids)
    development_counts = Counter(row.expected.action for row in development_rows)
    holdout_counts = Counter(row.expected.action for row in holdout_rows)
    if dict(sorted(development_counts.items())) != dict(sorted(DEVELOPMENT_COUNTS.items())):
        raise EvaluationHarnessError("development split counts are not deterministic")
    if dict(sorted(holdout_counts.items())) != dict(sorted(HOLDOUT_COUNTS.items())):
        raise EvaluationHarnessError("holdout split counts are not deterministic")
    payload = {
        "algorithm": SPLIT_ALGORITHM,
        "salt_id": "architecture-draft-v0.1-split",
        "source_file_sha256": source_hash,
        "sample_rows": EXPECTED_SAMPLE_ROWS,
        "development_rows": len(development_rows),
        "sealed_holdout_rows": len(holdout_rows),
        "development_counts": dict(sorted(DEVELOPMENT_COUNTS.items())),
        "holdout_counts": dict(sorted(HOLDOUT_COUNTS.items())),
        "holdout_ids_are_sealed": True,
    }
    return SplitPlan(
        rows=rows,
        development_rows=development_rows,
        holdout_rows=holdout_rows,
        source_file_sha256=source_hash,
        manifest_payload=payload,
        manifest_sha256=canonical_hash(payload),
    )


class EvaluationHarness:
    """Evaluator boundary that exposes only sanitized inputs by default."""

    def __init__(self, sample_path: str | Path):
        self.sample_path = Path(sample_path).resolve()
        if not self.sample_path.is_file():
            raise EvaluationHarnessError("sample_messages.csv does not exist")
        rows = _load_labeled_samples(self.sample_path)
        self._split = _split_rows(rows, sha256_file(self.sample_path))

    @property
    def split_manifest_sha256(self) -> str:
        return self._split.manifest_sha256

    def split_manifest(self) -> dict[str, object]:
        """Return counts and hashes only; no row IDs or labels."""

        return dict(self._split.manifest_payload) | {
            "manifest_sha256": self._split.manifest_sha256
        }

    def split_counts(self) -> dict[str, dict[str, int]]:
        return {
            "development": dict(sorted(DEVELOPMENT_COUNTS.items())),
            "holdout": dict(sorted(HOLDOUT_COUNTS.items())),
        }

    def router_inputs(self, partition: str = "development") -> tuple[SanitizedMessage, ...]:
        """Return label-free rows in source order for the routing boundary."""

        if partition == "development":
            return tuple(row.input for row in self._split.development_rows)
        if partition == "holdout":
            raise HoldoutSealedError("holdout inputs require an explicit reveal")
        raise EvaluationHarnessError("unknown evaluation partition")

    def reveal_holdout_inputs(self) -> tuple[SanitizedMessage, ...]:
        """Explicit evaluator-only hook for a post-freeze sealed run."""

        return tuple(row.input for row in self._split.holdout_rows)

    def _expected(self, partition: str, *, reveal_holdout: bool = False) -> tuple[ExpectedDecision, ...]:
        if partition == "development":
            return tuple(row.expected for row in self._split.development_rows)
        if partition == "holdout" and reveal_holdout:
            return tuple(row.expected for row in self._split.holdout_rows)
        if partition == "holdout":
            raise HoldoutSealedError("holdout labels require an explicit reveal")
        raise EvaluationHarnessError("unknown evaluation partition")

    def _inputs_for_partition(
        self, partition: str, *, reveal_holdout: bool = False
    ) -> tuple[SanitizedMessage, ...]:
        if partition == "development":
            return self.router_inputs("development")
        if partition == "holdout" and reveal_holdout:
            return self.reveal_holdout_inputs()
        if partition == "holdout":
            raise HoldoutSealedError("holdout inputs require an explicit reveal")
        raise EvaluationHarnessError("unknown evaluation partition")

    def run_manifest(
        self,
        partition: str = "development",
        *,
        configuration: Mapping[str, object] | None = None,
        reveal_holdout: bool = False,
    ) -> RunManifest:
        inputs = self._inputs_for_partition(partition, reveal_holdout=reveal_holdout)
        expected = self._expected(partition, reveal_holdout=reveal_holdout)
        action_counts = Counter(row.action for row in expected)
        sanitized_hash = canonical_hash([row.as_dict() for row in inputs])
        return build_run_manifest(
            partition=partition,
            source_file_sha256=self._split.source_file_sha256,
            sanitized_input_sha256=sanitized_hash,
            split_manifest_sha256=self._split.manifest_sha256,
            row_count=len(inputs),
            action_counts=action_counts,
            configuration=configuration or {},
        )

    def evaluate(
        self,
        predictions: Sequence[RawPrediction],
        *,
        artifact_root: str | Path,
        allowlists: Mapping[str, tuple[str, ...]] | None = None,
        partition: str = "development",
        configuration: Mapping[str, object] | None = None,
        reveal_holdout: bool = False,
    ) -> EvaluationRun:
        """Persist raw proposals once, then compute evaluator-only metrics."""

        expected = self._expected(partition, reveal_holdout=reveal_holdout)
        manifest = self.run_manifest(
            partition,
            configuration=configuration,
            reveal_holdout=reveal_holdout,
        )
        metrics = compute_metrics(expected, predictions, allowlists)
        store = ImmutableArtifactStore(Path(artifact_root) / manifest.run_id)
        store.write_run_bundle(manifest, predictions, metrics)
        return EvaluationRun(
            manifest=manifest,
            metrics=metrics,
            artifact_directory=store.root,
        )


def sanitize_sample_messages(
    sample_path: str | Path,
    *,
    partition: str = "development",
    reveal_holdout: bool = False,
) -> tuple[SanitizedMessage, ...]:
    """Convenience API used by tests and future evaluator entry points."""

    harness = EvaluationHarness(sample_path)
    if partition == "development":
        return harness.router_inputs(partition)
    if partition == "holdout" and reveal_holdout:
        return harness.reveal_holdout_inputs()
    if partition == "holdout":
        raise HoldoutSealedError("holdout inputs require an explicit reveal")
    raise EvaluationHarnessError("unknown evaluation partition")

"""Canonical, append-only run manifests and raw prediction artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from .predictions import RawPrediction


class ImmutableArtifactError(RuntimeError):
    """Raised when a run artifact would be overwritten or escape its root."""


def _jsonable(value: object) -> object:
    """Convert immutable mappings and sequences into JSON-native values."""
    if isinstance(value, MappingABC):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(item) for item in value)
    return value


def freeze_json(value: object) -> object:
    """Recursively freeze JSON-shaped values for immutable runtime contracts."""
    if isinstance(value, MappingABC):
        return MappingProxyType(
            {str(key): freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(freeze_json(item) for item in value)
    return value


def thaw_json(value: object) -> object:
    """Return a mutable JSON-shaped copy for callers and serializers."""
    if isinstance(value, MappingABC):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(thaw_json(item) for item in value)
    return value


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


@dataclass(frozen=True, slots=True)
class RunManifest:
    """A label-free identity record for one immutable evaluation partition."""

    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_json(self.payload))

    @property
    def run_id(self) -> str:
        return self.payload["run_id"]

    def as_dict(self) -> dict[str, object]:
        return thaw_json(self.payload)


def build_run_manifest(
    *,
    partition: str,
    source_file_sha256: str,
    sanitized_input_sha256: str,
    split_manifest_sha256: str,
    row_count: int,
    action_counts: Mapping[str, int],
    configuration: Mapping[str, object],
    architecture_version: str = "0.1",
    milestone: str = "M2",
) -> RunManifest:
    """Build a deterministic manifest without row labels or holdout IDs."""

    config_hash = canonical_hash(configuration)
    identity = {
        "architecture_version": architecture_version,
        "milestone": milestone,
        "partition": partition,
        "source_file_sha256": source_file_sha256,
        "sanitized_input_sha256": sanitized_input_sha256,
        "split_manifest_sha256": split_manifest_sha256,
        "row_count": row_count,
        "action_counts": dict(sorted(action_counts.items())),
        "configuration_sha256": config_hash,
        "label_visibility": "router_inputs_only",
        "raw_artifacts": ["manifest.json", "raw_predictions.jsonl", "metrics.json"],
    }
    run_id = canonical_hash(identity)
    return RunManifest(payload={**identity, "run_id": run_id})


def build_label_free_run_manifest(
    *,
    partition: str,
    source_file_sha256: str,
    sanitized_input_sha256: str,
    split_manifest_sha256: str,
    row_count: int,
    configuration: Mapping[str, object],
    architecture_version: str = "0.1",
    milestone: str = "M4A",
) -> RunManifest:
    """Build a runtime manifest without reading or recording expected labels."""

    config_hash = canonical_hash(configuration)
    identity = {
        "architecture_version": architecture_version,
        "milestone": milestone,
        "partition": partition,
        "source_file_sha256": source_file_sha256,
        "sanitized_input_sha256": sanitized_input_sha256,
        "split_manifest_sha256": split_manifest_sha256,
        "row_count": row_count,
        "action_counts": "label_isolated",
        "configuration_sha256": config_hash,
        "label_visibility": "router_inputs_only",
        "raw_artifacts": [
            "manifest.json",
            "raw_predictions.jsonl",
            "metrics.json",
            "rows/",
            "errors.jsonl",
        ],
    }
    return RunManifest(payload={**identity, "run_id": canonical_hash(identity)})


class ImmutableArtifactStore:
    """Write-once files rooted inside a single run directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, relative_name: str) -> Path:
        candidate = Path(relative_name)
        if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
            raise ImmutableArtifactError("artifact path escapes run root")
        path = (self.root / candidate).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ImmutableArtifactError("artifact path escapes run root") from exc
        return path

    def write_bytes(self, relative_name: str, content: bytes) -> Path:
        path = self._path(relative_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(content)
        except FileExistsError as exc:
            raise ImmutableArtifactError(f"artifact already exists: {relative_name}") from exc
        return path

    def write_json(self, relative_name: str, value: object) -> Path:
        return self.write_bytes(relative_name, canonical_json_bytes(value) + b"\n")

    def write_raw_predictions(
        self, relative_name: str, predictions: Iterable[RawPrediction]
    ) -> Path:
        lines = b"".join(canonical_json_bytes(prediction.as_dict()) + b"\n" for prediction in predictions)
        return self.write_bytes(relative_name, lines)

    def write_run_bundle(
        self,
        manifest: RunManifest,
        predictions: Iterable[RawPrediction],
        metrics: Mapping[str, object],
    ) -> Path:
        self.write_json("manifest.json", manifest.as_dict())
        self.write_raw_predictions("raw_predictions.jsonl", predictions)
        self.write_json("metrics.json", metrics)
        return self.root

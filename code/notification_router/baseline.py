"""Milestone 4A: one immutable, label-isolated development baseline runner."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from .artifacts import (
    ImmutableArtifactStore,
    RunManifest,
    build_label_free_run_manifest,
    canonical_hash,
    canonical_json_bytes,
    sha256_file,
)
from .confidence import CONFIDENCE_POLICY_VERSION
from .config import IntegrationConfig, IntegrationConfigError
from .contracts import (
    EXTRACTION_STATE_VALUES,
    ExtractionRecord,
    extraction_response_schema,
    routing_response_schema,
)
from .dataset import load_context_dataset, normalize_dataset
from .evaluation import EvaluationHarness
from .extraction_cache import (
    EXTRACTION_CACHE_VERSION,
    CachedExtraction,
    ExtractionCache,
    ExtractionCacheIdentity,
    build_extraction_cache_identity,
)
from .features import (
    FEATURES_VERSION,
    DeterministicFeatures,
    SafetyConstraints,
    compute_deterministic_features,
)
from .finalization import (
    FinalDecision,
    SAFETY_INVARIANTS_VERSION,
    degraded_final_decision,
    finalize_routing_decision,
    validate_routing_safety,
)
from .inputs import SanitizedMessage
from .integration import IntegrationError, IntegrationResult, ModelIntegrationClient
from .media import MediaSniffResult, sniff_media_file
from .metrics import compute_metrics
from .packet import PacketValidationError, RoutingPacket, assemble_routing_packet
from .predictions import RawPrediction
from .providers import ExtractionRequest, ProviderBundle, build_provider_bundle
from .retrieval import RetrievalConfig, RetrievalResult, retrieve_history
from .schemas import ACTION_VALUES
from .smoke import _read_env_file
from .telemetry import AttemptAccounting, CallAccounting, RedactedCallLogger, redact_text, safe_identifier


BASELINE_RUNNER_VERSION = "milestone4a-runner-v0"
SYSTEMATIC_CONTRACT_CODES = frozenset(
    {
        "ROUTER_SCHEMA_INVALID",
        "EVIDENCE_NOT_ALLOWED",
        "ACTION_CONSTRAINT_VIOLATION",
        "SEMANTIC_FLAG_CONTRADICTION",
        "PACKET_SCHEMA_INVALID",
        "EXTRACTOR_SCHEMA_INVALID",
    }
)


class BaselineConfigurationError(ValueError):
    """Raised before a baseline when inputs or configuration are unsafe."""


class BaselineAbortedError(RuntimeError):
    """Raised when a complete baseline cannot be defended."""


@dataclass(frozen=True, slots=True)
class BaselineRunnerConfig:
    artifact_root: Path
    cache_root: Path
    total_cost_limit_usd: float = 1.0
    systematic_contract_failure_limit: int = 3
    retrieval_config: RetrievalConfig = field(default_factory=RetrievalConfig)
    run_nonce: str | None = None

    def __post_init__(self) -> None:
        if self.total_cost_limit_usd < 0 or self.total_cost_limit_usd > 1.0:
            raise BaselineConfigurationError("baseline total cost must be in [0, 1.0]")
        if self.systematic_contract_failure_limit < 1:
            raise BaselineConfigurationError("systematic contract failure limit must be positive")
        if self.run_nonce is not None:
            if (
                not self.run_nonce
                or len(self.run_nonce) > 64
                or any(
                    character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
                    for character in self.run_nonce
                )
            ):
                raise BaselineConfigurationError(
                    "run_nonce must be a non-empty safe identifier of at most 64 characters"
                )

    def as_dict(self) -> dict[str, object]:
        result = {
            "runner_version": BASELINE_RUNNER_VERSION,
            "total_cost_limit_usd": self.total_cost_limit_usd,
            "systematic_contract_failure_limit": self.systematic_contract_failure_limit,
            "retrieval": self.retrieval_config.as_dict(),
        }
        if self.run_nonce is not None:
            result["run_nonce"] = self.run_nonce
        return result


@dataclass(frozen=True, slots=True)
class BaselineError:
    run_id: str
    message_id: str | None
    stage: str
    code: str
    severity: str
    retryable: bool
    attempt: int
    fallback: str
    details: str
    cause_hash: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        message_id: str | None,
        stage: str,
        code: str,
        severity: str,
        retryable: bool,
        attempt: int,
        fallback: str,
        detail: object,
    ) -> "BaselineError":
        bounded = redact_text(detail)
        cause_hash = canonical_hash({"stage": stage, "code": code, "detail": bounded})
        return cls(
            run_id=run_id,
            message_id=message_id,
            stage=stage,
            code=code,
            severity=severity,
            retryable=retryable,
            attempt=attempt,
            fallback=fallback,
            details=bounded,
            cause_hash=cause_hash,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "message_id": self.message_id,
            "stage": self.stage,
            "code": self.code,
            "severity": self.severity,
            "retryable": self.retryable,
            "attempt": self.attempt,
            "fallback": self.fallback,
            "details": self.details,
            "cause_hash": self.cause_hash,
        }


@dataclass(frozen=True, slots=True)
class BaselineRun:
    manifest: RunManifest
    artifact_directory: Path
    metrics: Mapping[str, object]
    predictions: tuple[RawPrediction, ...]
    completed_rows: int
    failed_rows: int
    degraded_rows: int
    aborted: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.manifest.run_id,
            "artifact_directory": str(self.artifact_directory),
            "completed_rows": self.completed_rows,
            "failed_rows": self.failed_rows,
            "degraded_rows": self.degraded_rows,
            "aborted": self.aborted,
            "metrics": dict(self.metrics),
        }


def _accounting_dict(accounting: CallAccounting | None, *, cache_hit: bool = False) -> dict[str, object]:
    if accounting is None:
        return {
            "attempt_count": 0,
            "latency_ms": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "attempts": [],
            "cache_hit": cache_hit,
        }
    result = accounting.as_dict()
    result["cache_hit"] = cache_hit
    return result


def _stage_totals(records: Sequence[Mapping[str, object]], stage: str) -> dict[str, object]:
    selected = [record for record in records if record.get("stage") == stage]
    latencies = [float(record.get("latency_ms", 0.0)) for record in selected]
    inputs = [int(record.get("input_tokens", 0) or 0) for record in selected]
    outputs = [int(record.get("output_tokens", 0) or 0) for record in selected]
    totals = [int(record.get("total_tokens", 0) or 0) for record in selected]
    costs = [float(record.get("cost_usd", 0.0) or 0.0) for record in selected]
    attempts = [int(record.get("attempt_count", 0) or 0) for record in selected]
    ordered = sorted(latencies)
    p50 = ordered[(len(ordered) - 1) // 2] if ordered else None
    p95 = ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))] if ordered else None
    return {
        "call_count": len(selected),
        "latency_ms": {
            "count": len(ordered),
            "mean": sum(ordered) / len(ordered) if ordered else None,
            "p50": p50,
            "p95": p95,
        },
        "input_tokens": sum(inputs),
        "output_tokens": sum(outputs),
        "total_tokens": sum(totals),
        "cost_usd": sum(costs),
        "attempts": sum(attempts),
        "retries": sum(max(attempt - 1, 0) for attempt in attempts),
        "cache_hits": sum(bool(record.get("cache_hit")) for record in selected),
    }


def _normalize_error_code(stage: str, code: str) -> str:
    if stage == "S3":
        return {
            "SCHEMA_INVALID": "EXTRACTOR_SCHEMA_INVALID",
            "JSON_INVALID": "EXTRACTOR_SCHEMA_INVALID",
            "PROVIDER_TIMEOUT": "EXTRACTOR_TIMEOUT",
            "PROVIDER_RATE_LIMITED": "EXTRACTOR_RATE_LIMITED",
            "PROVIDER_UNAVAILABLE": "EXTRACTOR_UNAVAILABLE",
        }.get(code, code)
    if stage == "S7":
        return {
            "SCHEMA_INVALID": "ROUTER_SCHEMA_INVALID",
            "JSON_INVALID": "ROUTER_SCHEMA_INVALID",
            "PROVIDER_TIMEOUT": "ROUTER_TIMEOUT",
            "PROVIDER_RATE_LIMITED": "ROUTER_RATE_LIMITED",
            "PROVIDER_UNAVAILABLE": "ROUTER_UNAVAILABLE",
            "PROVIDER_REQUEST_FAILED": "ROUTER_UNAVAILABLE",
        }.get(code, code)
    return code


def _fallback_for_error(stage: str) -> str:
    if stage == "S3":
        return "MEDIA_DEGRADED"
    if stage == "S5":
        return "RETRIEVAL_NON_EMBEDDING"
    return "DEGRADED_DIGEST_UNKNOWN"


def _severity_for(code: str) -> str:
    if code in {"MEDIA_MISSING", "MEDIA_UNSUPPORTED", "MEDIA_EMPTY_EXTRACTION", "CACHE_CORRUPT"}:
        return "warning"
    return "error"


def _safe_media_result(
    dataset_root: Path,
    message: SanitizedMessage,
    tables: object,
) -> tuple[MediaSniffResult, bytes]:
    if message.media_type == "image":
        metadata = next(
            (row for row in tables.images if row.image_id == message.media_id), None
        )
        declared_path = metadata.file_path if metadata is not None else ""
    elif message.media_type == "voice":
        metadata = next(
            (row for row in tables.voice_notes if row.voice_note_id == message.media_id), None
        )
        declared_path = metadata.file_path if metadata is not None else ""
    else:
        raise BaselineConfigurationError("text messages do not have media sniff requests")
    if metadata is None:
        return (
            MediaSniffResult(
                media_id=message.media_id or "",
                declared_media_type=message.media_type,
                declared_path=declared_path,
                resolved_path=None,
                byte_length=None,
                detected_format="unknown",
                extension_format=None,
                signature_state="missing",
                format_matches_media_type=None,
                error="MEDIA_MISSING",
            ),
            b"",
        )
    result = sniff_media_file(
        dataset_root,
        media_id=message.media_id or "",
        declared_media_type=message.media_type,
        declared_path=declared_path,
    )
    if result.resolved_path and Path(result.resolved_path).is_file():
        return result, Path(result.resolved_path).read_bytes()
    return result, b""


def _extraction_request(
    message: SanitizedMessage,
    sniff: MediaSniffResult,
    media_bytes: bytes,
) -> ExtractionRequest:
    if sniff.signature_state == "recognized" and sniff.format_matches_media_type is not False:
        source_state = "ready"
    elif sniff.signature_state == "missing":
        source_state = "missing"
    else:
        source_state = "unsupported"
    content_hash = hashlib.sha256(media_bytes).hexdigest() if media_bytes else None
    return ExtractionRequest(
        media_id=message.media_id or sniff.media_id,
        declared_media_type=sniff.declared_media_type,
        declared_path=sniff.declared_path,
        detected_format=sniff.detected_format,
        content_sha256=content_hash,
        media_bytes=media_bytes,
        created_at=message.created_at,
        source_media_state=source_state,
    )


def _fallback_extraction_record(
    request: ExtractionRequest,
    *,
    state: str,
    reason: str,
    extractor_name: str,
) -> ExtractionRecord:
    if state not in EXTRACTION_STATE_VALUES:
        raise BaselineConfigurationError("invalid fallback extraction state")
    config_hash = canonical_hash(
        {"extractor": extractor_name, "reason": reason, "version": BASELINE_RUNNER_VERSION}
    )
    return ExtractionRecord(
        media_id=request.media_id,
        content_sha256=request.content_sha256,
        declared_path=request.declared_path,
        detected_format=request.detected_format,
        media_state=state,
        extractor_name=extractor_name,
        extractor_version=BASELINE_RUNNER_VERSION,
        extractor_config_sha256=config_hash,
        extraction_schema_version="extraction-record-v0",
        extracted_text="",
        factual_description="",
        language="und",
        quality_score=0.05,
        quality_reasons=(reason,),
        created_at=request.created_at,
    )


def _rebind_cached_record(record: ExtractionRecord, request: ExtractionRequest) -> ExtractionRecord:
    """Bind cached semantic fields to this request without changing raw cache bytes."""

    return ExtractionRecord(
        media_id=request.media_id,
        content_sha256=record.content_sha256,
        declared_path=request.declared_path,
        detected_format=request.detected_format,
        media_state=record.media_state,
        extractor_name=record.extractor_name,
        extractor_version=record.extractor_version,
        extractor_config_sha256=record.extractor_config_sha256,
        extraction_schema_version=record.extraction_schema_version,
        extracted_text=record.extracted_text,
        factual_description=record.factual_description,
        language=record.language,
        quality_score=record.quality_score,
        quality_reasons=record.quality_reasons,
        created_at=request.created_at,
    )


def _manifest_configuration(
    config: IntegrationConfig,
    runner_config: BaselineRunnerConfig,
) -> dict[str, object]:
    return {
        "baseline_runner_version": BASELINE_RUNNER_VERSION,
        "provider": config.as_dict(),
        "runner": runner_config.as_dict(),
        "features_version": FEATURES_VERSION,
        "safety_invariants_version": SAFETY_INVARIANTS_VERSION,
        "confidence_policy_version": CONFIDENCE_POLICY_VERSION,
        "extraction_cache_version": EXTRACTION_CACHE_VERSION,
        "extraction_schema_sha256": canonical_hash(extraction_response_schema()),
        "routing_schema_sha256": canonical_hash(routing_response_schema()),
        "label_visibility": "router_inputs_only",
    }


def _row_accounting(
    extraction_accounting: Mapping[str, object],
    routing_accounting: Mapping[str, object],
) -> dict[str, object]:
    return {
        "latency_ms": float(extraction_accounting.get("latency_ms", 0.0) or 0.0)
        + float(routing_accounting.get("latency_ms", 0.0) or 0.0),
        "input_tokens": int(extraction_accounting.get("input_tokens", 0) or 0)
        + int(routing_accounting.get("input_tokens", 0) or 0),
        "output_tokens": int(extraction_accounting.get("output_tokens", 0) or 0)
        + int(routing_accounting.get("output_tokens", 0) or 0),
        "total_tokens": int(extraction_accounting.get("total_tokens", 0) or 0)
        + int(routing_accounting.get("total_tokens", 0) or 0),
        "cost_usd": float(extraction_accounting.get("cost_usd", 0.0) or 0.0)
        + float(routing_accounting.get("cost_usd", 0.0) or 0.0),
        "retries": max(int(extraction_accounting.get("attempt_count", 0) or 0) - 1, 0)
        + max(int(routing_accounting.get("attempt_count", 0) or 0) - 1, 0),
    }


def _write_jsonl(store: ImmutableArtifactStore, relative_name: str, rows: Sequence[Mapping[str, object]]) -> None:
    content = b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    store.write_bytes(relative_name, content)


def run_development_baseline(
    *,
    dataset_dir: str | Path,
    config: IntegrationConfig,
    runner_config: BaselineRunnerConfig,
    bundle: ProviderBundle | None = None,
    environment: Mapping[str, str] | None = None,
    log_file: str | Path | None = None,
) -> BaselineRun:
    """Run exactly the 20-row development partition through S0-S9."""

    dataset_root = Path(dataset_dir).resolve()
    sample_path = dataset_root / "sample_messages.csv"
    if sample_path.name != "sample_messages.csv" or not sample_path.is_file():
        raise BaselineConfigurationError("baseline requires dataset/sample_messages.csv")
    if config.cost_limit_usd > runner_config.total_cost_limit_usd:
        config = replace(config, cost_limit_usd=runner_config.total_cost_limit_usd)
    if bundle is None:
        bundle = build_provider_bundle(config, environ=environment)

    harness = EvaluationHarness(sample_path)
    development_rows = harness.router_inputs("development")
    if len(development_rows) != 20:
        raise BaselineConfigurationError("development baseline requires exactly 20 sanitized rows")
    tables = load_context_dataset(dataset_root)
    normalized = normalize_dataset(tables)
    configuration = _manifest_configuration(config, runner_config)
    manifest = build_label_free_run_manifest(
        partition="development",
        source_file_sha256=sha256_file(sample_path),
        sanitized_input_sha256=canonical_hash([row.as_dict() for row in development_rows]),
        split_manifest_sha256=harness.split_manifest_sha256,
        row_count=len(development_rows),
        configuration=configuration,
        run_nonce=runner_config.run_nonce,
    )
    store = ImmutableArtifactStore(Path(runner_config.artifact_root) / manifest.run_id)
    store.write_json("manifest.json", manifest.as_dict())
    cache = ExtractionCache(runner_config.cache_root)
    logger = RedactedCallLogger(path=log_file) if log_file else None
    active_row_hash: str | None = None
    active_raw_paths: dict[str, list[str]] = defaultdict(list)

    def raw_response_sink(
        call_id: str,
        stage: str,
        operation: str,
        attempt: int,
        metadata: Mapping[str, object],
        raw_response: bytes,
    ) -> None:
        del call_id, stage, metadata
        if active_row_hash is None:
            raise BaselineConfigurationError("raw response arrived without an active row")
        relative = f"rows/{active_row_hash}/{operation}-attempt-{attempt:02d}.json"
        store.write_bytes(relative, raw_response)
        active_raw_paths[operation].append(relative)

    client = ModelIntegrationClient(
        config,
        extraction_provider=bundle.extraction,
        routing_provider=bundle.routing,
        logger=logger,
        raw_response_sink=raw_response_sink,
    )
    predictions: list[RawPrediction] = []
    allowlists: dict[str, tuple[str, ...]] = {}
    row_records: list[Mapping[str, object]] = []
    error_records: list[BaselineError] = []
    accounting_records: list[Mapping[str, object]] = []
    extraction_states: Counter[str] = Counter()
    cache_hits = 0
    cache_misses = 0
    cache_corrupt = 0
    contract_failures: Counter[str] = Counter()
    aborted = False

    def record_error(
        *,
        message_id: str | None,
        stage: str,
        code: str,
        detail: object,
        retryable: bool = False,
        attempt: int = 1,
        fallback: str | None = None,
    ) -> BaselineError:
        normalized = _normalize_error_code(stage, code)
        error = BaselineError.create(
            run_id=manifest.run_id,
            message_id=message_id,
            stage=stage,
            code=normalized,
            severity=_severity_for(normalized),
            retryable=retryable,
            attempt=attempt,
            fallback=fallback or _fallback_for_error(stage),
            detail=detail,
        )
        error_records.append(error)
        if normalized in SYSTEMATIC_CONTRACT_CODES:
            contract_failures[normalized] += 1
        return error

    for message in development_rows:
        active_row_hash = safe_identifier(message.message_id)
        active_raw_paths.clear()
        row_errors: list[BaselineError] = []
        extraction_record: ExtractionRecord | None = None
        extraction_accounting: dict[str, object]
        sniff: MediaSniffResult | None = None
        cache_identity: ExtractionCacheIdentity | None = None
        cache_hit = False
        cache_corrupt_for_row = False
        extraction_source = "not_applicable"
        retrieval: RetrievalResult
        packet: RoutingPacket | None = None
        features: DeterministicFeatures | None = None
        constraints: SafetyConstraints | None = None
        routing_accounting: dict[str, object] = _accounting_dict(None)
        raw_schema_valid = False
        final: FinalDecision | None = None

        try:
            features, constraints = compute_deterministic_features(message, tables, normalized)
        except Exception as exc:
            row_errors.append(
                record_error(
                    message_id=message.message_id,
                    stage="S4",
                    code="FEATURE_COMPUTE_FAILED",
                    detail=type(exc).__name__,
                )
            )
            aborted = True
            break

        if message.media_type is None:
            extraction_accounting = _accounting_dict(None)
            extraction_source = "not_applicable"
        else:
            try:
                sniff, media_bytes = _safe_media_result(dataset_root, message, tables)
                request = _extraction_request(message, sniff, media_bytes)
                if sniff.signature_state == "missing":
                    extraction_record = _fallback_extraction_record(
                        request,
                        state="missing",
                        reason="media_missing",
                        extractor_name="deterministic-media-state",
                    )
                    extraction_accounting = _accounting_dict(None)
                    extraction_source = "media_state"
                    row_errors.append(
                        record_error(
                            message_id=message.message_id,
                            stage="S3",
                            code="MEDIA_MISSING",
                            detail="media metadata or file missing",
                        )
                    )
                elif sniff.signature_state != "recognized" or sniff.format_matches_media_type is False:
                    extraction_record = _fallback_extraction_record(
                        request,
                        state="unsupported",
                        reason="media_signature_unsupported",
                        extractor_name="deterministic-media-state",
                    )
                    extraction_accounting = _accounting_dict(None)
                    extraction_source = "media_state"
                    row_errors.append(
                        record_error(
                            message_id=message.message_id,
                            stage="S3",
                            code="MEDIA_UNSUPPORTED",
                            detail=sniff.error or "media signature unsupported",
                        )
                    )
                else:
                    cache_identity = build_extraction_cache_identity(
                        request,
                        provider_name=getattr(bundle.extraction, "name", type(bundle.extraction).__name__),
                        model_name=config.extraction_model,
                        backend=config.gemini_backend,
                        timeout_seconds=config.timeout_seconds,
                    )
                    lookup = cache.lookup(cache_identity) if cache_identity is not None else None
                    if lookup is not None and lookup.corrupt:
                        cache_corrupt += 1
                        cache_corrupt_for_row = True
                        row_errors.append(
                            record_error(
                                message_id=message.message_id,
                                stage="S3",
                                code="CACHE_CORRUPT",
                                detail="cache entry quarantined",
                            )
                        )
                    if lookup is not None and lookup.value is not None:
                        cache_hits += 1
                        cache_hit = True
                        cached = lookup.value
                        extraction_record = _rebind_cached_record(cached.record, request)
                        extraction_accounting = _accounting_dict(None, cache_hit=True)
                        extraction_source = "cache"
                        for attempt, raw in enumerate(cached.raw_responses, start=1):
                            relative = f"rows/{active_row_hash}/extraction-attempt-{attempt:02d}.json"
                            store.write_bytes(relative, raw)
                            active_raw_paths["extraction"].append(relative)
                    else:
                        cache_misses += 1
                        extraction_result: IntegrationResult[ExtractionRecord]
                        try:
                            extraction_result = client.extract(request)
                            extraction_record = extraction_result.value
                            extraction_accounting = _accounting_dict(extraction_result.accounting)
                            extraction_source = "provider"
                            if cache_identity is not None:
                                cache.put(
                                    cache_identity,
                                    extraction_record,
                                    extraction_result.raw_responses,
                                )
                        except IntegrationError as exc:
                            if exc.code == "COST_LIMIT_EXCEEDED":
                                raise BaselineAbortedError("configured total cost ceiling reached") from exc
                            row_errors.append(
                                record_error(
                                    message_id=message.message_id,
                                    stage="S3",
                                    code=exc.code,
                                    detail=exc.detail,
                                    attempt=exc.accounting.attempt_count,
                                )
                            )
                            extraction_record = _fallback_extraction_record(
                                request,
                                state="empty_extraction",
                                reason="extractor_failed",
                                extractor_name=getattr(bundle.extraction, "name", "extractor"),
                            )
                            extraction_accounting = _accounting_dict(exc.accounting)
                            extraction_source = "degraded"
            except BaselineAbortedError:
                raise
            except Exception as exc:
                row_errors.append(
                    record_error(
                        message_id=message.message_id,
                        stage="S3",
                        code="FEATURE_COMPUTE_FAILED",
                        detail=type(exc).__name__,
                    )
                )
                extraction_record = _fallback_extraction_record(
                    _extraction_request(
                        message,
                        sniff
                        or MediaSniffResult(
                            media_id=message.media_id or "",
                            declared_media_type=message.media_type,
                            declared_path="",
                            resolved_path=None,
                            byte_length=None,
                            detected_format="unknown",
                            extension_format=None,
                            signature_state="missing",
                            format_matches_media_type=None,
                        ),
                        b"",
                    ),
                    state="missing",
                    reason="media_processing_failed",
                    extractor_name="deterministic-media-state",
                )
                extraction_accounting = _accounting_dict(None)
                extraction_source = "degraded"

        if extraction_record is None and message.media_type is not None:
            raise BaselineAbortedError("media row did not produce an extraction record")
        extraction_states[
            extraction_record.media_state if extraction_record is not None else "not_applicable"
        ] += 1

        try:
            retrieval = retrieve_history(message, normalized, runner_config.retrieval_config)
            allowlists[message.message_id] = retrieval.allowed_evidence_message_ids
        except Exception as exc:
            row_errors.append(
                record_error(
                    message_id=message.message_id,
                    stage="S5",
                    code="RETRIEVER_FAILED",
                    detail=type(exc).__name__,
                )
            )
            aborted = True
            break

        try:
            packet = assemble_routing_packet(
                message,
                tables,
                normalized,
                retrieval,
                media_results=(sniff,) if sniff is not None else (),
                extraction_records=(extraction_record,) if extraction_record is not None else (),
                deterministic_features=features.as_dict() if features else None,
                safety_constraints=constraints.as_dict() if constraints else None,
            )
        except PacketValidationError as exc:
            row_errors.append(
                record_error(
                    message_id=message.message_id,
                    stage="S6",
                    code="PACKET_SCHEMA_INVALID",
                    detail=str(exc),
                )
            )
            aborted = True
            break

        try:
            routing_result = client.route(
                packet,
                decision_validator=lambda decision, packet_value: validate_routing_safety(
                    message=message,
                    packet=packet,
                    decision=decision,
                    features=features,
                    retrieval=retrieval,
                ),
            )
            routing_accounting = _accounting_dict(routing_result.accounting)
            raw_schema_valid = True
            final = finalize_routing_decision(
                message=message,
                packet=packet,
                decision=routing_result.value,
                features=features,
                extraction_record=extraction_record,
                retrieval=retrieval,
                routing_attempt_count=routing_result.accounting.attempt_count,
            )
        except IntegrationError as exc:
            if exc.code == "COST_LIMIT_EXCEEDED":
                raise BaselineAbortedError("configured total cost ceiling reached") from exc
            normalized_code = _normalize_error_code("S7", exc.code)
            row_errors.append(
                record_error(
                    message_id=message.message_id,
                    stage="S7",
                    code=normalized_code,
                    detail=exc.detail,
                    attempt=exc.accounting.attempt_count,
                )
            )
            routing_accounting = _accounting_dict(exc.accounting)
            final = degraded_final_decision(
                message=message,
                packet=packet,
                features=features,
                extraction_record=extraction_record,
                retrieval=retrieval,
                error_code=normalized_code,
            )

        if final is None:
            raise BaselineAbortedError("row did not produce a final decision")
        accounting = _row_accounting(extraction_accounting, routing_accounting)
        prediction = final.as_prediction(error_code=(row_errors[0].code if row_errors else None))
        prediction = replace(
            prediction,
            latency_ms=accounting["latency_ms"],
            cost_usd=accounting["cost_usd"],
        )
        predictions.append(prediction)
        accounting_records.append(
            {
                "message_id": message.message_id,
                "stage": "S3+S7",
                **accounting,
            }
        )
        error_count_before = len(error_records)
        row_errors.extend(error for error in error_records[error_count_before:] if error.message_id == message.message_id)
        row_record = {
            "message_id": message.message_id,
            "message_hash": safe_identifier(message.message_id),
            "media": {
                "declared_type": message.media_type,
                "detected_format": sniff.detected_format if sniff else None,
                "signature_state": sniff.signature_state if sniff else "not_applicable",
                "media_state": extraction_record.media_state if extraction_record else "not_applicable",
            },
            "extraction": {
                "source": extraction_source,
                "cache_key": cache_identity.key if cache_identity else None,
                "cache_hit": cache_hit,
                "cache_corrupt": cache_corrupt_for_row,
                "record": extraction_record.as_dict() if extraction_record else None,
                "accounting": extraction_accounting,
                "raw_response_artifacts": list(active_raw_paths.get("extract", ()))
                + list(active_raw_paths.get("extraction", ())),
            },
            "retrieval": {
                "candidate_count": len(retrieval.candidates),
                "allowlist_count": len(retrieval.allowed_evidence_message_ids),
                "config": retrieval.config.as_dict(),
            },
            "routing_packet": {
                "sha256": packet.sha256(),
                "bytes": len(packet.prompt_bytes()),
                "validated": True,
            },
            "routing": {
                "raw_schema_valid": raw_schema_valid,
                "accounting": routing_accounting,
                "raw_response_artifacts": list(active_raw_paths.get("route", ()))
                + list(active_raw_paths.get("routing", ())),
                "final_decision": final.as_dict(),
            },
            "row_accounting": accounting,
            "errors": [error.as_dict() for error in row_errors],
        }
        store.write_json(f"rows/{active_row_hash}/record.json", row_record)
        row_records.append(row_record)
        if final.degraded:
            # The row remains completed but is explicitly counted as degraded.
            pass
        if any(error.code in SYSTEMATIC_CONTRACT_CODES for error in row_errors):
            if any(
                contract_failures[error.code] >= runner_config.systematic_contract_failure_limit
                for error in row_errors
            ):
                aborted = True
                break

    active_row_hash = None
    if aborted:
        abort_payload = {
            "status": "aborted",
            "completed_rows": len(predictions),
            "expected_rows": len(development_rows),
            "reason": "systematic contract or required-stage failure",
            "error_counts": dict(sorted(contract_failures.items())),
        }
        store.write_json("abort.json", abort_payload)
        _write_jsonl(store, "errors.jsonl", [error.as_dict() for error in error_records])
        return BaselineRun(
            manifest=manifest,
            artifact_directory=store.root,
            metrics=abort_payload,
            predictions=tuple(predictions),
            completed_rows=len(predictions),
            failed_rows=len(error_records),
            degraded_rows=sum(
                bool(record.get("routing", {}).get("final_decision", {}).get("degraded"))
                for record in row_records
            ),
            aborted=True,
        )

    # The evaluator reads development labels only after the label-free router
    # pipeline has completed.  No expected value is passed into any stage above.
    expected = harness._expected("development")
    metrics = compute_metrics(expected, predictions, allowlists)
    stage_records = [
        {
            "stage": "S3",
            **dict(record.get("extraction", {}).get("accounting", {})),
        }
        for record in row_records
        if isinstance(record.get("extraction"), Mapping)
    ] + [
        {
            "stage": "S7",
            **dict(record.get("routing", {}).get("accounting", {})),
        }
        for record in row_records
        if isinstance(record.get("routing"), Mapping)
    ]
    error_by_stage = Counter(error.stage for error in error_records)
    error_by_code = Counter(error.code for error in error_records)
    metrics["baseline"] = {
        "completed_rows": len(predictions),
        "failed_rows": sum(bool(record.get("errors")) for record in row_records),
        "degraded_rows": sum(
            bool(record.get("routing", {}).get("final_decision", {}).get("degraded"))
            for record in row_records
        ),
        "raw_model_rows": sum(bool(record.get("routing", {}).get("raw_schema_valid")) for record in row_records),
        "raw_model_schema_valid_rate": (
            sum(bool(record.get("routing", {}).get("raw_schema_valid")) for record in row_records)
            / len(development_rows)
            if development_rows
            else 0.0
        ),
        "contract_final_rows": len(predictions),
        "extraction_states": dict(sorted(extraction_states.items())),
        "extraction_cache": {
            "hits": cache_hits,
            "misses": cache_misses,
            "corrupt_entries": cache_corrupt,
        },
        "operations": {
            "extraction": _stage_totals(stage_records, "S3"),
            "routing": _stage_totals(stage_records, "S7"),
            "row_total_cost_usd": sum(float(record["cost_usd"]) for record in accounting_records),
            "row_total_latency_ms": sum(float(record["latency_ms"]) for record in accounting_records),
            "row_total_tokens": sum(int(record["total_tokens"]) for record in accounting_records),
            "row_total_retries": sum(int(record["retries"]) for record in accounting_records),
            "provider_total_cost_usd": client.total_cost_usd,
            "configured_total_cost_limit_usd": runner_config.total_cost_limit_usd,
        },
        "error_taxonomy": {
            "by_stage": dict(sorted(error_by_stage.items())),
            "by_code": dict(sorted(error_by_code.items())),
            "records": len(error_records),
        },
        "artifact_integrity": {
            "raw_attempts_preserved": True,
            "packet_hashes_preserved": True,
            "final_decisions_preserved": True,
            "labels_visible_to_router": False,
            "holdout_accessed": False,
            "target_messages_accessed": False,
        },
    }
    store.write_raw_predictions("raw_predictions.jsonl", predictions)
    _write_jsonl(store, "errors.jsonl", [error.as_dict() for error in error_records])
    store.write_json("metrics.json", metrics)
    failed_rows = sum(bool(record.get("errors")) for record in row_records)
    degraded_rows = sum(
        bool(record.get("routing", {}).get("final_decision", {}).get("degraded"))
        for record in row_records
    )
    return BaselineRun(
        manifest=manifest,
        artifact_directory=store.root,
        metrics=metrics,
        predictions=tuple(predictions),
        completed_rows=len(predictions),
        failed_rows=failed_rows,
        degraded_rows=degraded_rows,
        aborted=False,
    )


def _load_vertex_configuration(env_file: Path, max_cost_usd: float) -> tuple[IntegrationConfig, Mapping[str, str]]:
    environment = dict(os.environ)
    environment.update(_read_env_file(env_file))
    config = IntegrationConfig.from_env(environment)
    if not config.api_enabled:
        raise BaselineConfigurationError("Vertex baseline requires explicit API enablement")
    if config.provider_name != "gemini" or config.gemini_backend != "vertex":
        raise BaselineConfigurationError("baseline requires NOTIFICATION_ROUTER_GEMINI_BACKEND=vertex")
    return replace(config, cost_limit_usd=min(config.cost_limit_usd, max_cost_usd)), environment


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=Path("../dataset"))
    parser.add_argument("--env-file", type=Path, default=Path("../.env"))
    parser.add_argument("--artifact-dir", type=Path, default=Path("../.artifacts/milestone4a"))
    parser.add_argument("--cache-dir", type=Path, default=Path("../.artifacts/milestone4a/cache"))
    parser.add_argument("--max-cost-usd", type=float, default=1.0)
    parser.add_argument(
        "--run-id",
        dest="run_nonce",
        help="explicit safe namespace for a fresh immutable rerun",
    )
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    import sys

    args = build_parser().parse_args(argv)
    try:
        config, environment = _load_vertex_configuration(args.env_file, args.max_cost_usd)
        runner_config = BaselineRunnerConfig(
            artifact_root=args.artifact_dir,
            cache_root=args.cache_dir,
            total_cost_limit_usd=args.max_cost_usd,
            run_nonce=args.run_nonce,
        )
        result = run_development_baseline(
            dataset_dir=args.dataset_dir,
            config=config,
            runner_config=runner_config,
            environment=environment,
            log_file=args.log_file,
        )
    except (BaselineConfigurationError, IntegrationConfigError, BaselineAbortedError) as exc:
        print(f"baseline failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if not result.aborted else 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

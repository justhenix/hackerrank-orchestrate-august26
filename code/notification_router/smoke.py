"""Bounded development-sample smoke runner for fake or explicitly enabled providers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .artifacts import ImmutableArtifactStore
from .config import IntegrationConfig, IntegrationConfigError
from .dataset import load_context_dataset, normalize_dataset
from .evaluation import EvaluationHarness
from .integration import IntegrationError, ModelIntegrationClient
from .packet import assemble_routing_packet
from .providers import ExtractionRequest, ProviderBundle, build_provider_bundle
from .retrieval import retrieve_history
from .telemetry import RedactedCallLogger, safe_identifier
from .media import MediaSniffResult, sniff_media_file
from .inputs import SanitizedMessage


class SmokeConfigurationError(ValueError):
    """Raised when the smoke command would leave the development boundary."""


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _read_env_file(path: str | Path) -> dict[str, str]:
    """Read a small dotenv subset without printing or expanding its values."""

    env_path = Path(path)
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise SmokeConfigurationError("environment file could not be read") from None
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise SmokeConfigurationError(f"environment file line {line_number} is invalid")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not _ENV_NAME_RE.fullmatch(name):
            raise SmokeConfigurationError(f"environment file line {line_number} has an invalid name")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def _artifact_run_id() -> str:
    return datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S.%fZ")


def _persist_raw_responses(
    store: ImmutableArtifactStore | None,
    *,
    run_id: str,
    message_id: str,
    operation: str,
    raw_responses: tuple[bytes, ...],
) -> list[str]:
    if store is None:
        return []
    paths: list[str] = []
    for attempt, raw_response in enumerate(raw_responses, start=1):
        relative_path = _persist_raw_response(
            store,
            run_id=run_id,
            message_id=message_id,
            operation=operation,
            attempt=attempt,
            raw_response=raw_response,
        )
        paths.append(relative_path)
    return paths


def _persist_raw_response(
    store: ImmutableArtifactStore | None,
    *,
    run_id: str,
    message_id: str,
    operation: str,
    attempt: int,
    raw_response: bytes,
) -> str:
    if store is None:
        raise SmokeConfigurationError("raw response persistence requires an artifact store")
    message_hash = safe_identifier(message_id) or "unknown-message"
    relative_path = f"{run_id}/{message_hash}/{operation}-attempt-{attempt:02d}.json"
    store.write_bytes(relative_path, raw_response)
    return relative_path


def _sample_path(dataset_dir: Path) -> Path:
    return (dataset_dir / "sample_messages.csv").resolve()


def _select_development_samples(
    rows: Iterable[SanitizedMessage],
) -> tuple[tuple[str, SanitizedMessage], ...]:
    selected: list[tuple[str, SanitizedMessage]] = []
    for kind, media_type in (("text", None), ("image", "image"), ("voice", "voice")):
        row = next((candidate for candidate in rows if candidate.media_type == media_type), None)
        if row is not None:
            selected.append((kind, row))
    return tuple(selected)


def _sniff_for_message(
    dataset_root: Path, message: SanitizedMessage, tables: object
) -> tuple[MediaSniffResult, bytes]:
    if message.media_type == "image":
        metadata = next(row for row in tables.images if row.image_id == message.media_id)
        media_id, media_type, path = metadata.image_id, "image", metadata.file_path
    elif message.media_type == "voice":
        metadata = next(row for row in tables.voice_notes if row.voice_note_id == message.media_id)
        media_id, media_type, path = metadata.voice_note_id, "voice", metadata.file_path
    else:
        raise SmokeConfigurationError("text samples do not have media requests")
    result = sniff_media_file(
        dataset_root,
        media_id=media_id,
        declared_media_type=media_type,
        declared_path=path,
    )
    if result.resolved_path is None or not Path(result.resolved_path).is_file():
        return result, b""
    return result, Path(result.resolved_path).read_bytes()


def _extraction_request(
    message: SanitizedMessage, result: MediaSniffResult, media_bytes: bytes
) -> ExtractionRequest:
    if result.signature_state == "recognized":
        source_state = "ready"
    elif result.signature_state == "missing":
        source_state = "missing"
    else:
        source_state = "unsupported"
    content_sha256 = hashlib.sha256(media_bytes).hexdigest() if media_bytes else None
    return ExtractionRequest(
        media_id=message.media_id or result.media_id,
        declared_media_type=result.declared_media_type,
        declared_path=result.declared_path,
        detected_format=result.detected_format,
        content_sha256=content_sha256,
        media_bytes=media_bytes,
        created_at=message.created_at,
        source_media_state=source_state,
    )


def run_smoke(
    *,
    dataset_dir: str | Path,
    config: IntegrationConfig,
    bundle: ProviderBundle,
    log_file: str | Path | None = None,
    artifact_dir: str | Path | None = None,
) -> dict[str, object]:
    """Process at most one text, image, and voice development sample."""

    root = Path(dataset_dir).resolve()
    sample_file = _sample_path(root)
    harness = EvaluationHarness(sample_file)
    development_rows = harness.router_inputs("development")
    selected = _select_development_samples(development_rows)
    tables = load_context_dataset(root)
    normalized = normalize_dataset(tables)
    artifact_store = ImmutableArtifactStore(artifact_dir) if artifact_dir else None
    artifact_run_id = _artifact_run_id() if artifact_store else None
    persisted_artifacts: dict[tuple[str, str], list[str]] = {}

    def raw_response_sink(
        call_id: str,
        stage: str,
        operation: str,
        attempt: int,
        metadata: dict[str, object],
        raw_response: bytes,
    ) -> None:
        del call_id, stage
        if artifact_store is None or artifact_run_id is None:
            return
        artifact_key = next(
            (
                value
                for name in ("message_id", "media_id")
                if isinstance(value := metadata.get(name), str) and value
            ),
            "unknown-message",
        )
        relative_path = _persist_raw_response(
            artifact_store,
            run_id=artifact_run_id,
            message_id=artifact_key,
            operation=operation,
            attempt=attempt,
            raw_response=raw_response,
        )
        persisted_artifacts.setdefault((artifact_key, operation), []).append(relative_path)

    def artifact_paths(message_id: str | None, operation: str) -> list[str]:
        if not message_id:
            return []
        return list(persisted_artifacts.get((message_id, operation), ()))

    logger = RedactedCallLogger(path=log_file) if log_file else None
    client = ModelIntegrationClient(
        config,
        extraction_provider=bundle.extraction,
        routing_provider=bundle.routing,
        logger=logger,
        raw_response_sink=raw_response_sink,
    )
    output: list[dict[str, object]] = []
    for kind, message in selected:
        sniff_result: MediaSniffResult | None = None
        extraction_result = None
        extraction_artifacts: list[str] = []
        media_results: tuple[MediaSniffResult, ...] = ()
        if message.media_type is not None:
            sniff_result, media_bytes = _sniff_for_message(root, message, tables)
            media_results = (sniff_result,)
            try:
                extraction_result = client.extract(
                    _extraction_request(message, sniff_result, media_bytes)
                )
            except IntegrationError:
                extraction_artifacts = artifact_paths(message.media_id, "extraction")
                raise
            extraction_artifacts = artifact_paths(message.media_id, "extraction")
        retrieval = retrieve_history(message, normalized)
        packet = assemble_routing_packet(
            message,
            tables,
            normalized,
            retrieval,
            media_results=media_results,
            extraction_records=(extraction_result.value,) if extraction_result else (),
        )
        try:
            routing_result = client.route(packet)
        except IntegrationError:
            routing_artifacts = artifact_paths(message.message_id, "routing")
            raise
        routing_artifacts = artifact_paths(message.message_id, "routing")
        packet_data = packet.as_dict()
        allowed_evidence = tuple(packet_data["allowed_evidence_message_ids"])
        selected_evidence = tuple(routing_result.value.selected_evidence_message_ids)
        extraction_record = extraction_result.value if extraction_result else None
        output.append(
            {
                "kind": kind,
                "message_hash": safe_identifier(message.message_id),
                "detected_media_format": (
                    sniff_result.detected_format if sniff_result else None
                ),
                "media_signature_state": (
                    sniff_result.signature_state if sniff_result else None
                ),
                "media_state": (
                    packet_data["media"]["media_state"]
                    if isinstance(packet_data.get("media"), dict)
                    else None
                ),
                "extraction": (
                    {
                        "schema_valid": True,
                        "media_state": extraction_record.media_state,
                        "quality_score": extraction_record.quality_score,
                        "quality_reasons": list(extraction_record.quality_reasons),
                        "extracted_text_chars": len(extraction_record.extracted_text),
                        "factual_description_chars": len(
                            extraction_record.factual_description
                        ),
                    }
                    if extraction_record
                    else None
                ),
                "routing_packet": {
                    "validated": True,
                    "sha256": packet.sha256(),
                    "bytes": len(packet.prompt_bytes()),
                },
                "routing": {
                    "action": routing_result.value.action,
                    "message_type": routing_result.value.message_type,
                    "routing_uncertainty": routing_result.value.routing_uncertainty,
                    "schema_valid": True,
                    "selected_evidence_message_ids": list(selected_evidence),
                    "allowed_evidence_message_ids": list(allowed_evidence),
                    "evidence_within_allowlist": set(selected_evidence).issubset(
                        set(allowed_evidence)
                    ),
                },
                "extraction_accounting": (
                    extraction_result.accounting.as_dict() if extraction_result else None
                ),
                "routing_accounting": routing_result.accounting.as_dict(),
                "raw_response_artifacts": extraction_artifacts + routing_artifacts,
            }
        )
    result = {
        "provider": config.provider_name,
        "api_enabled": config.api_enabled,
        "development_samples_processed": len(output),
        "sample_kinds": [row["kind"] for row in output],
        "estimated_max_cost_usd": config.maximum_smoke_cost(logical_calls=6),
        "actual_cost_usd": client.total_cost_usd,
        "results": output,
    }
    if artifact_store is not None:
        result["artifact_directory"] = str(artifact_store.root)
        result["artifact_run_id"] = artifact_run_id
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=Path("../dataset"))
    parser.add_argument("--provider", choices=("fake", "http-json", "gemini"))
    parser.add_argument("--enable-api", action="store_true")
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        environment = dict(os.environ)
        if args.env_file is not None:
            environment.update(_read_env_file(args.env_file))
        config = IntegrationConfig.from_env(environment)
        changes: dict[str, object] = {}
        if args.provider is not None:
            changes["provider_name"] = args.provider
        if args.enable_api:
            changes["api_enabled"] = True
        if args.max_cost_usd is not None:
            changes["cost_limit_usd"] = args.max_cost_usd
        if changes:
            config = replace(config, **changes)
        if config.provider_name != "fake" and not config.api_enabled:
            raise SmokeConfigurationError(
                "live providers require --enable-api or NOTIFICATION_ROUTER_API_ENABLED=1"
            )
        bundle = build_provider_bundle(config, environ=environment)
        result = run_smoke(
            dataset_dir=args.dataset_dir,
            config=config,
            bundle=bundle,
            log_file=args.log_file,
            artifact_dir=args.artifact_dir,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (IntegrationConfigError, SmokeConfigurationError, IntegrationError) as exc:
        error = {
            "error_code": getattr(exc, "code", "SMOKE_CONFIG_INVALID"),
            "detail": str(exc),
        }
        print(json.dumps(error, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

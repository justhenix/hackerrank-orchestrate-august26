"""Bounded development-sample smoke runner for fake or explicitly enabled providers."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Iterable

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
) -> dict[str, object]:
    """Process at most one text, image, and voice development sample."""

    root = Path(dataset_dir).resolve()
    sample_file = _sample_path(root)
    harness = EvaluationHarness(sample_file)
    development_rows = harness.router_inputs("development")
    selected = _select_development_samples(development_rows)
    tables = load_context_dataset(root)
    normalized = normalize_dataset(tables)
    logger = RedactedCallLogger(path=log_file) if log_file else None
    client = ModelIntegrationClient(
        config,
        extraction_provider=bundle.extraction,
        routing_provider=bundle.routing,
        logger=logger,
    )
    output: list[dict[str, object]] = []
    for kind, message in selected:
        sniff_result: MediaSniffResult | None = None
        extraction_result = None
        media_results: tuple[MediaSniffResult, ...] = ()
        if message.media_type is not None:
            sniff_result, media_bytes = _sniff_for_message(root, message, tables)
            media_results = (sniff_result,)
            extraction_result = client.extract(
                _extraction_request(message, sniff_result, media_bytes)
            )
        retrieval = retrieve_history(message, normalized)
        packet = assemble_routing_packet(
            message,
            tables,
            normalized,
            retrieval,
            media_results=media_results,
            extraction_records=(extraction_result.value,) if extraction_result else (),
        )
        routing_result = client.route(packet)
        output.append(
            {
                "kind": kind,
                "message_hash": safe_identifier(message.message_id),
                "media_state": (
                    packet.as_dict()["media"]["media_state"]
                    if isinstance(packet.as_dict().get("media"), dict)
                    else None
                ),
                "routing": {
                    "action": routing_result.value.action,
                    "message_type": routing_result.value.message_type,
                    "routing_uncertainty": routing_result.value.routing_uncertainty,
                },
                "extraction_accounting": (
                    extraction_result.accounting.as_dict() if extraction_result else None
                ),
                "routing_accounting": routing_result.accounting.as_dict(),
            }
        )
    return {
        "provider": config.provider_name,
        "api_enabled": config.api_enabled,
        "development_samples_processed": len(output),
        "sample_kinds": [row["kind"] for row in output],
        "estimated_max_cost_usd": config.maximum_smoke_cost(logical_calls=6),
        "actual_cost_usd": client.total_cost_usd,
        "results": output,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=Path("../dataset"))
    parser.add_argument("--provider", choices=("fake", "http-json", "gemini"))
    parser.add_argument("--enable-api", action="store_true")
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = IntegrationConfig.from_env()
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
        bundle = build_provider_bundle(config)
        result = run_smoke(
            dataset_dir=args.dataset_dir,
            config=config,
            bundle=bundle,
            log_file=args.log_file,
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

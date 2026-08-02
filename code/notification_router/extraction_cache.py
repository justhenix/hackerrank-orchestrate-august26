"""Write-once content-addressed extraction cache."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .artifacts import canonical_hash, canonical_json_bytes
from .contracts import ExtractionRecord, extraction_response_schema, parse_extraction_record
from .providers import ExtractionRequest


EXTRACTION_CACHE_VERSION = "extraction-cache-v0"


@dataclass(frozen=True, slots=True)
class ExtractionCacheIdentity:
    """All semantic inputs that determine reusable extraction output."""

    content_sha256: str
    detected_format: str
    extractor_name: str
    extractor_version: str
    extractor_config_sha256: str
    extraction_schema_version: str
    model_name: str

    def as_dict(self) -> dict[str, str]:
        return {
            "cache_version": EXTRACTION_CACHE_VERSION,
            "content_sha256": self.content_sha256,
            "detected_format": self.detected_format,
            "extractor_name": self.extractor_name,
            "extractor_version": self.extractor_version,
            "extractor_config_sha256": self.extractor_config_sha256,
            "extraction_schema_version": self.extraction_schema_version,
            "model_name": self.model_name,
        }

    @property
    def key(self) -> str:
        return canonical_hash(self.as_dict())


@dataclass(frozen=True, slots=True)
class CachedExtraction:
    identity: ExtractionCacheIdentity
    record: ExtractionRecord
    raw_responses: tuple[bytes, ...]


@dataclass(frozen=True, slots=True)
class ExtractionCacheLookup:
    key: str
    value: CachedExtraction | None
    corrupt: bool = False

    @property
    def hit(self) -> bool:
        return self.value is not None


def build_extraction_cache_identity(
    request: ExtractionRequest,
    *,
    provider_name: str,
    model_name: str,
    backend: str | None,
    timeout_seconds: float,
) -> ExtractionCacheIdentity | None:
    """Build an identity; missing media never receives a reusable entry."""

    if not request.content_sha256:
        return None
    extractor_config_sha256 = canonical_hash(
        {
            "provider": provider_name,
            "backend": backend,
            "timeout_seconds": timeout_seconds,
            "schema_sha256": canonical_hash(extraction_response_schema()),
        }
    )
    return ExtractionCacheIdentity(
        content_sha256=request.content_sha256,
        detected_format=request.detected_format,
        extractor_name=provider_name,
        extractor_version=EXTRACTION_CACHE_VERSION,
        extractor_config_sha256=extractor_config_sha256,
        extraction_schema_version="extraction-record-v0",
        model_name=model_name,
    )


class ExtractionCache:
    """Filesystem cache whose entries are immutable and content-addressed."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.entries = self.root / "entries"
        self.quarantine = self.root / "quarantine"
        self.entries.mkdir(parents=True, exist_ok=True)
        self.quarantine.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
            raise ValueError("cache key must be lowercase SHA-256")
        return self.entries / f"{key}.json"

    def lookup(self, identity: ExtractionCacheIdentity) -> ExtractionCacheLookup:
        path = self._path(identity.key)
        if not path.exists():
            return ExtractionCacheLookup(identity.key, None)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping) or payload.get("identity") != identity.as_dict():
                raise ValueError("cache identity mismatch")
            record_payload = payload.get("record")
            raw_values = payload.get("raw_responses")
            if not isinstance(record_payload, Mapping) or not isinstance(raw_values, list):
                raise ValueError("cache payload shape invalid")
            raw_responses = tuple(base64.b64decode(value, validate=True) for value in raw_values)
            record = parse_extraction_record(
                json.dumps(record_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )
            if record.content_sha256 != identity.content_sha256:
                raise ValueError("cache record content hash mismatch")
            return ExtractionCacheLookup(
                identity.key,
                CachedExtraction(identity=identity, record=record, raw_responses=raw_responses),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._quarantine(path)
            return ExtractionCacheLookup(identity.key, None, corrupt=True)

    def _quarantine(self, path: Path) -> None:
        if not path.exists():
            return
        target = self.quarantine / path.name
        suffix = 1
        while target.exists():
            target = self.quarantine / f"{path.stem}-{suffix}{path.suffix}"
            suffix += 1
        path.replace(target)

    def put(
        self,
        identity: ExtractionCacheIdentity,
        record: ExtractionRecord,
        raw_responses: tuple[bytes, ...],
    ) -> Path:
        if record.content_sha256 != identity.content_sha256:
            raise ValueError("cache record does not match identity")
        payload = {
            "identity": identity.as_dict(),
            "record": record.as_dict(),
            "raw_responses": [base64.b64encode(raw).decode("ascii") for raw in raw_responses],
        }
        path = self._path(identity.key)
        content = canonical_json_bytes(payload) + b"\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            # Another deterministic worker may have completed the same key.
            # Existing content is never overwritten or silently replaced.
            existing = self.lookup(identity)
            if existing.value is None:
                raise
        return path

"""Byte-signature media format sniffing without decoding or extraction."""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import DatasetTables


KNOWN_FORMATS = frozenset({"jpeg", "png", "webp", "avif", "mp3", "m4a", "wav"})
IMAGE_FORMATS = frozenset({"jpeg", "png", "webp", "avif"})
AUDIO_FORMATS = frozenset({"mp3", "m4a", "wav"})
SNIFF_HEADER_BYTES = 64


def _iso_bmff_brands(data: bytes) -> tuple[bytes, ...]:
    if len(data) < 16 or data[4:8] != b"ftyp":
        return ()
    compatible = tuple(data[offset : offset + 4] for offset in range(16, len(data) - 3, 4))
    return (data[8:12],) + compatible


def sniff_bytes(data: bytes) -> str:
    """Return the supported format identified from leading bytes only."""

    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    brands = set(_iso_bmff_brands(data))
    if brands & {b"avif", b"avis"}:
        return "avif"
    # The supplied voice fixtures include both M4A and MP4 audio brands.
    if brands & {b"M4A ", b"M4B ", b"mp42"}:
        return "m4a"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"ID3"):
        return "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        # MPEG audio frame sync; reject the reserved layer bits.
        if (data[1] & 0x06) != 0:
            return "mp3"
    return "unknown"


_EXTENSION_FORMATS = {
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".png": "png",
    ".webp": "webp",
    ".avif": "avif",
    ".mp3": "mp3",
    ".m4a": "m4a",
    ".wav": "wav",
}


@dataclass(frozen=True, slots=True)
class MediaSniffResult:
    media_id: str
    declared_media_type: str
    declared_path: str
    resolved_path: str | None
    byte_length: int | None
    detected_format: str
    extension_format: str | None
    signature_state: str
    format_matches_media_type: bool | None
    error: str | None = None


def _resolve_inside(root: Path, relative_path: str) -> Path:
    candidate = Path(relative_path)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ValueError("media path escapes the dataset directory")
    root_resolved = root.resolve()
    resolved = (root_resolved / candidate).resolve()
    resolved.relative_to(root_resolved)
    return resolved


def sniff_media_file(
    dataset_root: str | Path,
    *,
    media_id: str,
    declared_media_type: str,
    declared_path: str,
) -> MediaSniffResult:
    """Sniff one metadata path; no decoder or semantic extractor is called."""

    extension_format = _EXTENSION_FORMATS.get(Path(declared_path).suffix.lower())
    try:
        resolved = _resolve_inside(Path(dataset_root), declared_path)
    except (OSError, ValueError):
        return MediaSniffResult(
            media_id=media_id,
            declared_media_type=declared_media_type,
            declared_path=declared_path,
            resolved_path=None,
            byte_length=None,
            detected_format="unknown",
            extension_format=extension_format,
            signature_state="invalid_path",
            format_matches_media_type=None,
            error="MEDIA_PATH_INVALID",
        )

    if not resolved.is_file():
        return MediaSniffResult(
            media_id=media_id,
            declared_media_type=declared_media_type,
            declared_path=declared_path,
            resolved_path=str(resolved),
            byte_length=None,
            detected_format="unknown",
            extension_format=extension_format,
            signature_state="missing",
            format_matches_media_type=None,
            error="MEDIA_MISSING",
        )

    try:
        with resolved.open("rb") as handle:
            byte_length = os.fstat(handle.fileno()).st_size
            header = handle.read(SNIFF_HEADER_BYTES)
    except OSError:
        return MediaSniffResult(
            media_id=media_id,
            declared_media_type=declared_media_type,
            declared_path=declared_path,
            resolved_path=str(resolved),
            byte_length=None,
            detected_format="unknown",
            extension_format=extension_format,
            signature_state="read_error",
            format_matches_media_type=None,
            error="MEDIA_READ_FAILED",
        )

    detected = sniff_bytes(header)
    expected_formats = IMAGE_FORMATS if declared_media_type == "image" else AUDIO_FORMATS
    matches = detected in expected_formats if detected != "unknown" else None
    return MediaSniffResult(
        media_id=media_id,
        declared_media_type=declared_media_type,
        declared_path=declared_path,
        resolved_path=str(resolved),
        byte_length=byte_length,
        detected_format=detected,
        extension_format=extension_format,
        signature_state="recognized" if detected != "unknown" else "unknown",
        format_matches_media_type=matches,
        error=("MEDIA_TYPE_FORMAT_MISMATCH" if matches is False else None),
    )


def sniff_dataset_media(tables: DatasetTables) -> tuple[MediaSniffResult, ...]:
    """Sniff all declared media metadata rows in deterministic table order."""

    results: list[MediaSniffResult] = []
    results.extend(
        sniff_media_file(
            tables.dataset_root,
            media_id=media.image_id,
            declared_media_type="image",
            declared_path=media.file_path,
        )
        for media in tables.images
    )
    results.extend(
        sniff_media_file(
            tables.dataset_root,
            media_id=media.voice_note_id,
            declared_media_type="voice",
            declared_path=media.file_path,
        )
        for media in tables.voice_notes
    )
    return tuple(results)


def media_summary(results: Iterable[MediaSniffResult]) -> dict[str, object]:
    """Create a small deterministic, JSON-serializable media report."""

    materialized = tuple(results)
    return {
        "records": len(materialized),
        "signature_states": dict(sorted(Counter(r.signature_state for r in materialized).items())),
        "detected_formats": dict(sorted(Counter(r.detected_format for r in materialized).items())),
        "extension_mismatches": sum(
            r.extension_format is not None
            and r.detected_format != "unknown"
            and r.extension_format != r.detected_format
            for r in materialized
        ),
        "declaration_mismatches": sum(r.format_matches_media_type is False for r in materialized),
    }

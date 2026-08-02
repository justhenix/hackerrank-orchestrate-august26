"""Provider interfaces, offline fakes, and an opt-in generic HTTP adapter."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Callable, Mapping, Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request

from .config import IntegrationConfig, IntegrationConfigError
from .artifacts import canonical_json_bytes
from .packet import RoutingPacket


class ProviderConfigurationError(IntegrationConfigError):
    """Raised when a provider cannot be constructed safely."""


class ProviderCallError(RuntimeError):
    """Normalized provider transport failure without raw response content."""

    def __init__(self, code: str, detail: str, *, retryable: bool) -> None:
        super().__init__(detail)
        self.code = code
        self.retryable = retryable
        self.detail = detail


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer or null")
        if self.cost_usd is not None and (
            isinstance(self.cost_usd, bool)
            or not isinstance(self.cost_usd, (int, float))
            or self.cost_usd < 0
            or self.cost_usd != self.cost_usd
            or self.cost_usd in {float("inf"), float("-inf")}
        ):
            raise ValueError("cost_usd must be a finite non-negative number or null")

    def as_dict(self) -> dict[str, object]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
        }


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    raw_json: bytes
    usage: TokenUsage = TokenUsage()
    provider_request_id: str | None = None
    structured_json: bytes | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.raw_json, bytes):
            raise TypeError("raw_json must be immutable bytes")
        if self.structured_json is not None and not isinstance(self.structured_json, bytes):
            raise TypeError("structured_json must be immutable bytes or null")

    @property
    def output_json(self) -> bytes:
        return self.structured_json if self.structured_json is not None else self.raw_json


@dataclass(frozen=True, slots=True)
class ExtractionRequest:
    media_id: str
    declared_media_type: str
    declared_path: str
    detected_format: str
    content_sha256: str | None
    media_bytes: bytes
    created_at: datetime
    source_media_state: str = "ready"

    def __post_init__(self) -> None:
        if not isinstance(self.media_bytes, bytes):
            raise TypeError("media_bytes must be immutable bytes")
        if self.source_media_state == "ready" and self.content_sha256 is None:
            raise ValueError("ready extraction requests require a content hash")

    def redacted_metadata(self) -> dict[str, object]:
        return {
            "media_id": self.media_id,
            "declared_media_type": self.declared_media_type,
            "declared_path": self.declared_path,
            "detected_format": self.detected_format,
            "content_sha256": self.content_sha256,
            "created_at": self.created_at.isoformat(),
            "source_media_state": self.source_media_state,
            "media_bytes": len(self.media_bytes),
        }


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    packet: RoutingPacket
    validation_feedback: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.validation_feedback is not None:
            if not isinstance(self.validation_feedback, Mapping):
                raise TypeError("validation_feedback must be a mapping or null")
            object.__setattr__(
                self,
                "validation_feedback",
                MappingProxyType(dict(self.validation_feedback)),
            )

    @property
    def packet_bytes(self) -> bytes:
        if self.validation_feedback is None:
            return self.packet.prompt_bytes()
        envelope = self.packet.prompt_envelope()
        instructions = dict(envelope["instructions"])
        instructions["validation_feedback"] = dict(self.validation_feedback)
        envelope["instructions"] = instructions
        return canonical_json_bytes(envelope)

    def redacted_metadata(self) -> dict[str, object]:
        packet = self.packet.as_dict()
        message = packet.get("message", {})
        return {
            "message_id": message.get("message_id") if isinstance(message, Mapping) else None,
            "packet_sha256": self.packet.sha256(),
            "packet_bytes": len(self.packet_bytes),
        }


class MultimodalExtractionProvider(Protocol):
    name: str

    def extract(
        self,
        request: ExtractionRequest,
        *,
        model: str,
        timeout_seconds: float,
    ) -> ProviderResponse:
        ...


class TextRoutingProvider(Protocol):
    name: str

    def route(
        self,
        request: RoutingRequest,
        *,
        model: str,
        timeout_seconds: float,
    ) -> ProviderResponse:
        ...


class FakeMultimodalProvider:
    """Deterministic extraction fake with no network or credential access."""

    name = "fake"
    requires_api = False

    def __init__(
        self,
        *,
        response_factory: Callable[[ExtractionRequest], Mapping[str, object]] | None = None,
        failures_before_success: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self.response_factory = response_factory
        self.failures_before_success = failures_before_success
        self.cost_usd = cost_usd
        self.calls = 0

    def extract(
        self,
        request: ExtractionRequest,
        *,
        model: str,
        timeout_seconds: float,
    ) -> ProviderResponse:
        del model, timeout_seconds
        self.calls += 1
        if self.calls <= self.failures_before_success:
            raise ProviderCallError("EXTRACTOR_UNAVAILABLE", "fake transient extraction failure", retryable=True)
        if self.response_factory is not None:
            payload = dict(self.response_factory(request))
        else:
            state = "ok" if request.source_media_state == "ready" else request.source_media_state
            payload = {
                "media_id": request.media_id,
                "content_sha256": request.content_sha256,
                "declared_path": request.declared_path,
                "detected_format": request.detected_format,
                "media_state": state,
                "extractor_name": "fake-multimodal",
                "extractor_version": "fake-v0",
                "extractor_config_sha256": hashlib.sha256(b"fake-extractor-v0").hexdigest(),
                "extraction_schema_version": "decision-contract-v0.1",
                "extracted_text": "offline fake extraction" if state == "ok" else "",
                "factual_description": "offline fake media description" if state == "ok" else "",
                "language": "und",
                "quality_score": 0.5 if state == "ok" else 0.05,
                "quality_reasons": ["fake_provider"],
                "created_at": request.created_at.isoformat(),
            }
        input_tokens = max(1, len(request.media_bytes) // 4)
        return ProviderResponse(
            raw_json=json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=32,
                total_tokens=input_tokens + 32,
                cost_usd=self.cost_usd,
            ),
            provider_request_id=f"fake-extraction-{self.calls}",
        )


class FakeTextRoutingProvider:
    """Deterministic routing fake that emits a contract-shaped raw proposal."""

    name = "fake"
    requires_api = False

    def __init__(
        self,
        *,
        response_factory: Callable[[RoutingRequest], Mapping[str, object]] | None = None,
        failures_before_success: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self.response_factory = response_factory
        self.failures_before_success = failures_before_success
        self.cost_usd = cost_usd
        self.calls = 0
        self.requests: list[RoutingRequest] = []

    def route(
        self,
        request: RoutingRequest,
        *,
        model: str,
        timeout_seconds: float,
    ) -> ProviderResponse:
        del model, timeout_seconds
        self.calls += 1
        self.requests.append(request)
        if self.calls <= self.failures_before_success:
            raise ProviderCallError("ROUTER_UNAVAILABLE", "fake transient routing failure", retryable=True)
        if self.response_factory is not None:
            payload = dict(self.response_factory(request))
        else:
            payload = {
                "action": "digest",
                "message_type": "unknown",
                "reason": "Offline fake routing proposal.",
                "selected_evidence_message_ids": [],
                "routing_uncertainty": 0.5,
                "uncertainty_reasons": ["fake_provider"],
                "semantic_flags": {
                    "time_critical": False,
                    "indirectly_addresses_user": False,
                    "transactional": False,
                    "promotional": False,
                    "credential_or_secret_request": False,
                    "impersonation_or_domain_concern": False,
                    "suspicious_link_or_payment_request": False,
                    "warning_or_quoted_discussion": False,
                },
                "deadline_at": None,
                "semantic_support": [],
                "reported_contradictory_signal_count": 0,
            }
        input_tokens = max(1, len(request.packet_bytes) // 4)
        return ProviderResponse(
            raw_json=json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=64,
                total_tokens=input_tokens + 64,
                cost_usd=self.cost_usd,
            ),
            provider_request_id=f"fake-routing-{self.calls}",
        )


class HttpJsonProvider:
    """Provider-neutral JSON gateway; constructed only when API access is enabled."""

    name = "http-json"
    requires_api = True

    def __init__(
        self,
        *,
        api_key: str,
        extraction_url: str,
        routing_url: str,
    ) -> None:
        if not api_key:
            raise ProviderConfigurationError("API credential is empty")
        if not extraction_url or not routing_url:
            raise ProviderConfigurationError("both provider endpoints are required")
        self._api_key = api_key
        self._extraction_url = extraction_url
        self._routing_url = routing_url

    @staticmethod
    def _usage(value: object) -> TokenUsage:
        if not isinstance(value, Mapping):
            return TokenUsage()
        return TokenUsage(
            input_tokens=value.get("input_tokens"),
            output_tokens=value.get("output_tokens"),
            total_tokens=value.get("total_tokens"),
            cost_usd=value.get("cost_usd"),
        )

    def _post(
        self,
        url: str,
        payload: Mapping[str, object],
        *,
        timeout_seconds: float,
        operation: str,
    ) -> ProviderResponse:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        request = urllib_request.Request(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
                response_body = response.read()
                status = getattr(response, "status", 200)
        except urllib_error.HTTPError as exc:
            retryable = exc.code == 429 or exc.code >= 500
            raise ProviderCallError(
                "PROVIDER_RATE_LIMITED" if exc.code == 429 else "PROVIDER_REQUEST_FAILED",
                f"{operation} HTTP status {exc.code}",
                retryable=retryable,
            ) from None
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise ProviderCallError(
                "PROVIDER_TIMEOUT" if isinstance(exc, TimeoutError) else "PROVIDER_UNAVAILABLE",
                f"{operation} transport {type(exc).__name__}",
                retryable=True,
            ) from None
        if status < 200 or status >= 300:
            raise ProviderCallError(
                "PROVIDER_REQUEST_FAILED",
                f"{operation} HTTP status {status}",
                retryable=status >= 500,
            )
        try:
            envelope = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProviderCallError(
                "PROVIDER_RESPONSE_INVALID", f"{operation} response is not JSON", retryable=False
            ) from exc
        if not isinstance(envelope, Mapping):
            raise ProviderCallError(
                "PROVIDER_RESPONSE_INVALID", f"{operation} response is not an object", retryable=False
            )
        output = envelope.get("output", envelope.get("response"))
        if not isinstance(output, Mapping):
            raise ProviderCallError(
                "PROVIDER_RESPONSE_INVALID", f"{operation} output object is missing", retryable=False
            )
        return ProviderResponse(
            raw_json=response_body,
            usage=self._usage(envelope.get("usage")),
            provider_request_id=(
                envelope.get("request_id") if isinstance(envelope.get("request_id"), str) else None
            ),
            structured_json=json.dumps(output, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )

    def extract(
        self,
        request: ExtractionRequest,
        *,
        model: str,
        timeout_seconds: float,
    ) -> ProviderResponse:
        payload = {
            "model": model,
            "operation": "multimodal_extraction",
            "response_schema": "ExtractionRecord",
            "input": {
                "media_id": request.media_id,
                "declared_media_type": request.declared_media_type,
                "declared_path": request.declared_path,
                "detected_format": request.detected_format,
                "content_sha256": request.content_sha256,
                "created_at": request.created_at.isoformat(),
                "media_bytes_base64": base64.b64encode(request.media_bytes).decode("ascii"),
            },
        }
        return self._post(
            self._extraction_url,
            payload,
            timeout_seconds=timeout_seconds,
            operation="extraction",
        )

    def route(
        self,
        request: RoutingRequest,
        *,
        model: str,
        timeout_seconds: float,
    ) -> ProviderResponse:
        payload = {
            "model": model,
            "operation": "text_routing",
            "response_schema": "RawRoutingDecision",
            "input": json.loads(request.packet_bytes.decode("utf-8")),
        }
        return self._post(
            self._routing_url,
            payload,
            timeout_seconds=timeout_seconds,
            operation="routing",
        )


@dataclass(frozen=True, slots=True)
class ProviderBundle:
    extraction: MultimodalExtractionProvider
    routing: TextRoutingProvider


def build_provider_bundle(
    config: IntegrationConfig,
    *,
    environ: Mapping[str, str] | None = None,
    gemini_client_factory: Callable[..., object] | None = None,
    gemini_credentials_loader: Callable[..., object] | None = None,
    gemini_sdk: tuple[object, object] | None = None,
) -> ProviderBundle:
    """Build fake, generic HTTP, or explicitly selected Gemini providers."""

    if config.provider_name == "fake":
        return ProviderBundle(FakeMultimodalProvider(), FakeTextRoutingProvider())
    if config.provider_name == "gemini":
        from .gemini import build_gemini_provider_bundle

        return build_gemini_provider_bundle(
            config,
            environ=environ,
            client_factory=gemini_client_factory,
            credentials_loader=gemini_credentials_loader,
            sdk=gemini_sdk,
        )
    if config.provider_name != "http-json":
        raise ProviderConfigurationError(f"unknown provider {config.provider_name!r}")
    if not config.api_enabled:
        raise ProviderConfigurationError(
            "API calls are disabled; set NOTIFICATION_ROUTER_API_ENABLED=1 explicitly"
        )
    if not config.extraction_url or not config.routing_url:
        raise ProviderConfigurationError(
            "NOTIFICATION_ROUTER_EXTRACTION_URL and NOTIFICATION_ROUTER_ROUTING_URL are required"
        )
    credential = config.credential(environ)
    provider = HttpJsonProvider(
        api_key=credential,
        extraction_url=config.extraction_url,
        routing_url=config.routing_url,
    )
    return ProviderBundle(provider, provider)

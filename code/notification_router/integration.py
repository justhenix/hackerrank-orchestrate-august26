"""Bounded provider invocation, strict parsing, retries, budgets, and batches."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Generic, Mapping, Sequence, TypeVar

from .config import IntegrationConfig, IntegrationConfigError
from .contracts import (
    ExtractionRecord,
    RawRoutingDecision,
    StructuredOutputError,
    parse_extraction_record,
    parse_routing_decision,
    validate_extraction_record,
    validate_routing_decision_against_packet,
)
from .packet import RoutingPacket
from .providers import (
    ExtractionRequest,
    MultimodalExtractionProvider,
    ProviderCallError,
    ProviderResponse,
    RoutingRequest,
    TextRoutingProvider,
    TokenUsage,
)
from .telemetry import (
    AttemptAccounting,
    CallAccounting,
    NULL_LOGGER,
    RedactedCallLogger,
)


T = TypeVar("T")
RawResponseSink = Callable[[str, str, str, int, Mapping[str, object], bytes], None]
ValidationErrorSink = Callable[[StructuredOutputError], None]


class CostLimitExceeded(RuntimeError):
    """Raised before an attempt when the configured cost budget is exhausted."""


class IntegrationError(RuntimeError):
    """Normalized failure retaining bounded accounting and raw responses."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        accounting: CallAccounting,
        raw_responses: tuple[bytes, ...] = (),
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.accounting = accounting
        self.raw_responses = raw_responses


@dataclass(frozen=True, slots=True)
class IntegrationResult(Generic[T]):
    value: T
    raw_responses: tuple[bytes, ...]
    accounting: CallAccounting


class CostLedger:
    """Thread-safe cumulative cost ledger with pre-call reservations."""

    def __init__(self, limit_usd: float) -> None:
        self.limit_usd = limit_usd
        self._total_usd = 0.0
        self._reserved_usd = 0.0
        self._lock = threading.Lock()

    @property
    def total_usd(self) -> float:
        with self._lock:
            return self._total_usd

    def reserve(self, amount_usd: float) -> None:
        with self._lock:
            if self._total_usd + self._reserved_usd + amount_usd > self.limit_usd + 1e-12:
                raise CostLimitExceeded("configured cost limit would be exceeded")
            self._reserved_usd += amount_usd

    def settle(self, reservation_usd: float, actual_usd: float) -> None:
        with self._lock:
            self._reserved_usd = max(0.0, self._reserved_usd - reservation_usd)
            self._total_usd += actual_usd


class ModelIntegrationClient:
    """Provider-neutral client that owns all call-side policy and accounting."""

    def __init__(
        self,
        config: IntegrationConfig,
        *,
        extraction_provider: MultimodalExtractionProvider,
        routing_provider: TextRoutingProvider,
        logger: RedactedCallLogger | None = None,
        raw_response_sink: RawResponseSink | None = None,
        monotonic: Callable[[], float] = time.perf_counter,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.extraction_provider = extraction_provider
        self.routing_provider = routing_provider
        if not config.api_enabled and (
            getattr(extraction_provider, "requires_api", False)
            or getattr(routing_provider, "requires_api", False)
        ):
            raise IntegrationConfigError("API-backed providers require explicit API enablement")
        self.logger = logger or NULL_LOGGER
        self.raw_response_sink = raw_response_sink
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._ledger = CostLedger(config.cost_limit_usd)
        self._counter = 0
        self._counter_lock = threading.Lock()

    @property
    def total_cost_usd(self) -> float:
        return self._ledger.total_usd

    def _call_id(self, operation: str) -> str:
        with self._counter_lock:
            self._counter += 1
            return f"{operation}-{self._counter:04d}"

    def _cost_for_usage(self, usage: TokenUsage) -> float:
        if usage.cost_usd is not None:
            return float(usage.cost_usd)
        input_cost = (usage.input_tokens or 0) * self.config.input_cost_per_1k_tokens / 1000
        output_cost = (usage.output_tokens or 0) * self.config.output_cost_per_1k_tokens / 1000
        return input_cost + output_cost

    @staticmethod
    def _provider_name(provider: object) -> str:
        name = getattr(provider, "name", None)
        return name if isinstance(name, str) and name else type(provider).__name__

    def _invoke(
        self,
        *,
        stage: str,
        operation: str,
        model: str,
        provider: object,
        metadata: dict[str, object],
        provider_call: Callable[[], ProviderResponse],
        parser: Callable[[bytes], T],
        validation_error_sink: ValidationErrorSink | None = None,
    ) -> IntegrationResult[T]:
        call_id = self._call_id(operation)
        provider_name = self._provider_name(provider)
        attempts: list[AttemptAccounting] = []
        raw_responses: list[bytes] = []
        last_code = "PROVIDER_UNAVAILABLE"
        last_detail = "provider call failed"
        for attempt in range(1, self.config.maximum_attempts + 1):
            reservation = self.config.per_call_cost_limit_usd
            try:
                self._ledger.reserve(reservation)
            except CostLimitExceeded as exc:
                accounting = CallAccounting(
                    call_id=call_id,
                    stage=stage,
                    operation=operation,
                    provider=provider_name,
                    model=model,
                    attempts=tuple(attempts),
                )
                raise IntegrationError(
                    "COST_LIMIT_EXCEEDED",
                    str(exc),
                    accounting=accounting,
                    raw_responses=tuple(raw_responses),
                ) from exc
            self.logger.request_started(
                call_id=call_id,
                stage=stage,
                operation=operation,
                provider=provider_name,
                model=model,
                message_id=(metadata.get("message_id") if isinstance(metadata.get("message_id"), str) else None),
                media_id=(metadata.get("media_id") if isinstance(metadata.get("media_id"), str) else None),
                payload_bytes=int(metadata.get("packet_bytes", metadata.get("media_bytes", 0))),
            )
            started = self._monotonic()
            response: ProviderResponse | None = None
            settled = False
            try:
                response = provider_call()
                if not isinstance(response, ProviderResponse):
                    raise ProviderCallError(
                        "PROVIDER_RESPONSE_INVALID",
                        "provider returned an unexpected response object",
                        retryable=False,
                    )
                raw_responses.append(response.raw_json)
                if self.raw_response_sink is not None:
                    self.raw_response_sink(
                        call_id,
                        stage,
                        operation,
                        attempt,
                        metadata,
                        response.raw_json,
                    )
                actual_cost = self._cost_for_usage(response.usage)
                self._ledger.settle(reservation, actual_cost)
                settled = True
                latency_ms = (self._monotonic() - started) * 1000
                if actual_cost > self.config.per_call_cost_limit_usd + 1e-12:
                    raise CostLimitExceeded("provider response exceeded per-call cost limit")
                value = parser(response.output_json)
                accounting_attempt = AttemptAccounting(
                    attempt=attempt,
                    latency_ms=latency_ms,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    total_tokens=response.usage.total_tokens,
                    cost_usd=actual_cost,
                    success=True,
                )
                attempts.append(accounting_attempt)
                accounting = CallAccounting(
                    call_id=call_id,
                    stage=stage,
                    operation=operation,
                    provider=provider_name,
                    model=model,
                    attempts=tuple(attempts),
                )
                self.logger.request_finished(accounting=accounting)
                return IntegrationResult(
                    value=value,
                    raw_responses=tuple(raw_responses),
                    accounting=accounting,
                )
            except ProviderCallError as exc:
                if not settled:
                    self._ledger.settle(reservation, 0.0)
                    settled = True
                latency_ms = (self._monotonic() - started) * 1000
                last_code, last_detail = exc.code, exc.detail
                attempts.append(
                    AttemptAccounting(
                        attempt=attempt,
                        latency_ms=latency_ms,
                        input_tokens=response.usage.input_tokens if response else None,
                        output_tokens=response.usage.output_tokens if response else None,
                        total_tokens=response.usage.total_tokens if response else None,
                        cost_usd=self._cost_for_usage(response.usage) if response else 0.0,
                        success=False,
                        error_code=exc.code,
                    )
                )
                self.logger.request_error(
                    call_id=call_id,
                    stage=stage,
                    operation=operation,
                    provider=provider_name,
                    model=model,
                    attempt=attempt,
                    code=exc.code,
                    detail=exc.detail,
                )
                if not exc.retryable or attempt >= self.config.maximum_attempts:
                    break
            except CostLimitExceeded as exc:
                if not settled:
                    self._ledger.settle(reservation, 0.0)
                    settled = True
                latency_ms = (self._monotonic() - started) * 1000
                last_code, last_detail = "COST_LIMIT_EXCEEDED", str(exc)
                attempts.append(
                    AttemptAccounting(
                        attempt=attempt,
                        latency_ms=latency_ms,
                        input_tokens=response.usage.input_tokens if response else None,
                        output_tokens=response.usage.output_tokens if response else None,
                        total_tokens=response.usage.total_tokens if response else None,
                        cost_usd=self._cost_for_usage(response.usage) if response else 0.0,
                        success=False,
                        error_code="COST_LIMIT_EXCEEDED",
                    )
                )
                self.logger.request_error(
                    call_id=call_id,
                    stage=stage,
                    operation=operation,
                    provider=provider_name,
                    model=model,
                    attempt=attempt,
                    code="COST_LIMIT_EXCEEDED",
                    detail=str(exc),
                )
                break
            except StructuredOutputError as exc:
                if not settled:
                    self._ledger.settle(reservation, 0.0)
                    settled = True
                latency_ms = (self._monotonic() - started) * 1000
                actual_cost = self._cost_for_usage(response.usage) if response else 0.0
                last_code, last_detail = exc.code, str(exc)
                attempts.append(
                    AttemptAccounting(
                        attempt=attempt,
                        latency_ms=latency_ms,
                        input_tokens=response.usage.input_tokens if response else None,
                        output_tokens=response.usage.output_tokens if response else None,
                        total_tokens=response.usage.total_tokens if response else None,
                        cost_usd=actual_cost,
                        success=False,
                        error_code=exc.code,
                    )
                )
                self.logger.request_error(
                    call_id=call_id,
                    stage=stage,
                    operation=operation,
                    provider=provider_name,
                    model=model,
                    attempt=attempt,
                    code=exc.code,
                    detail=str(exc),
                )
                if validation_error_sink is not None:
                    validation_error_sink(exc)
                if attempt >= self.config.maximum_attempts:
                    break
            except Exception as exc:  # provider adapters must not leak raw errors
                if not settled:
                    self._ledger.settle(reservation, 0.0)
                    settled = True
                latency_ms = (self._monotonic() - started) * 1000
                last_code, last_detail = "PROVIDER_RESPONSE_INVALID", type(exc).__name__
                attempts.append(
                    AttemptAccounting(
                        attempt=attempt,
                        latency_ms=latency_ms,
                        input_tokens=response.usage.input_tokens if response else None,
                        output_tokens=response.usage.output_tokens if response else None,
                        total_tokens=response.usage.total_tokens if response else None,
                        cost_usd=self._cost_for_usage(response.usage) if response else 0.0,
                        success=False,
                        error_code=last_code,
                    )
                )
                self.logger.request_error(
                    call_id=call_id,
                    stage=stage,
                    operation=operation,
                    provider=provider_name,
                    model=model,
                    attempt=attempt,
                    code=last_code,
                    detail=type(exc).__name__,
                )
                break
            if attempt < self.config.maximum_attempts and self.config.retry_backoff_seconds:
                self._sleeper(self.config.retry_backoff_seconds)
        accounting = CallAccounting(
            call_id=call_id,
            stage=stage,
            operation=operation,
            provider=provider_name,
            model=model,
            attempts=tuple(attempts),
        )
        raise IntegrationError(
            last_code,
            last_detail,
            accounting=accounting,
            raw_responses=tuple(raw_responses),
        )

    def extract(self, request: ExtractionRequest) -> IntegrationResult[ExtractionRecord]:
        def parse(raw: bytes) -> ExtractionRecord:
            record = parse_extraction_record(raw)
            validate_extraction_record(
                record,
                media_id=request.media_id,
                declared_path=request.declared_path,
                detected_format=request.detected_format,
                content_sha256=request.content_sha256,
                created_at=request.created_at,
            )
            return record

        return self._invoke(
            stage="S3",
            operation="extract",
            model=self.config.extraction_model,
            provider=self.extraction_provider,
            metadata=request.redacted_metadata(),
            provider_call=lambda: self.extraction_provider.extract(
                request,
                model=self.config.extraction_model,
                timeout_seconds=self.config.timeout_seconds,
            ),
            parser=parse,
        )

    def route(self, packet: RoutingPacket) -> IntegrationResult[RawRoutingDecision]:
        validation_feedback: dict[str, str] | None = None
        packet_value = packet.as_dict()
        allowlist = packet_value.get("allowed_evidence_message_ids", ())
        if not isinstance(allowlist, (list, tuple)):
            raise IntegrationError(
                "PACKET_SCHEMA_INVALID",
                "packet evidence allowlist is invalid",
                accounting=CallAccounting(
                    call_id="route-0000",
                    stage="S7",
                    operation="route",
                    provider=self._provider_name(self.routing_provider),
                    model=self.config.routing_model,
                    attempts=(),
                ),
            )

        def parse(raw: bytes) -> RawRoutingDecision:
            decision = parse_routing_decision(
                raw,
                allowed_evidence_message_ids=tuple(allowlist),
            )
            validate_routing_decision_against_packet(decision, packet_value)
            return decision

        def provider_call() -> ProviderResponse:
            request = RoutingRequest(packet, validation_feedback=validation_feedback)
            return self.routing_provider.route(
                request,
                model=self.config.routing_model,
                timeout_seconds=self.config.timeout_seconds,
            )

        def validation_error_sink(error: StructuredOutputError) -> None:
            nonlocal validation_feedback
            validation_feedback = error.as_machine_readable()

        return self._invoke(
            stage="S7",
            operation="route",
            model=self.config.routing_model,
            provider=self.routing_provider,
            metadata=RoutingRequest(packet).redacted_metadata(),
            provider_call=provider_call,
            parser=parse,
            validation_error_sink=validation_error_sink,
        )

    def extract_batch(
        self, requests: Sequence[ExtractionRequest]
    ) -> tuple[IntegrationResult[ExtractionRecord], ...]:
        with ThreadPoolExecutor(max_workers=self.config.concurrency) as executor:
            futures = [executor.submit(self.extract, request) for request in requests]
            return tuple(future.result() for future in futures)

    def route_batch(
        self, packets: Sequence[RoutingPacket]
    ) -> tuple[IntegrationResult[RawRoutingDecision], ...]:
        with ThreadPoolExecutor(max_workers=self.config.concurrency) as executor:
            futures = [executor.submit(self.route, packet) for packet in packets]
            return tuple(future.result() for future in futures)

from __future__ import annotations

import io
import json
import hashlib
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from notification_router.config import IntegrationConfig, IntegrationConfigError
from notification_router.contracts import (
    MAX_REASON_CHARS,
    MAX_REASON_WORDS,
    StructuredOutputError,
    parse_extraction_record,
    parse_routing_decision,
    routing_response_schema,
    validate_routing_decision_against_packet,
)
from notification_router.dataset import (
    load_context_dataset,
    normalize_dataset,
)
import notification_router.dataset as dataset_module
from notification_router.evaluation import EvaluationHarness
from notification_router.integration import IntegrationError, ModelIntegrationClient
from notification_router.media import sniff_media_file
from notification_router.packet import assemble_routing_packet
from notification_router.providers import (
    ExtractionRequest,
    FakeMultimodalProvider,
    FakeTextRoutingProvider,
    ProviderConfigurationError,
    build_provider_bundle,
)
from notification_router.retrieval import retrieve_history
from notification_router.smoke import run_smoke
from notification_router.telemetry import RedactedCallLogger, safe_identifier


ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "dataset"
SAMPLE = DATASET / "sample_messages.csv"


class MilestoneThreeATests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.harness = EvaluationHarness(SAMPLE)
        cls.tables = load_context_dataset(DATASET)
        cls.normalized = normalize_dataset(cls.tables)

    def _text_packet(self):
        message = next(row for row in self.harness.router_inputs() if row.media_type is None)
        retrieval = retrieve_history(message, self.normalized)
        return message, assemble_routing_packet(
            message,
            self.tables,
            self.normalized,
            retrieval,
            media_results=(),
        )

    def test_context_loader_never_opens_target_messages(self) -> None:
        opened: list[str] = []
        original = dataset_module.load_csv_table

        def tracking_loader(path, table_name=None):
            opened.append(str(table_name or Path(path).name))
            return original(path, table_name)

        with patch.object(dataset_module, "load_csv_table", side_effect=tracking_loader):
            tables = load_context_dataset(DATASET)
        self.assertNotIn("messages.csv", opened)
        self.assertEqual(tables.messages, ())

    def test_environment_config_is_explicit_and_secret_free(self) -> None:
        config = IntegrationConfig.from_env(
            {
                "NOTIFICATION_ROUTER_API_ENABLED": "0",
                "NOTIFICATION_ROUTER_PROVIDER": "fake",
                "NOTIFICATION_ROUTER_EXTRACTION_MODEL": "extract-test-v1",
                "NOTIFICATION_ROUTER_ROUTING_MODEL": "route-test-v1",
                "NOTIFICATION_ROUTER_TIMEOUT_SECONDS": "12.5",
                "NOTIFICATION_ROUTER_MAX_RETRIES": "2",
                "NOTIFICATION_ROUTER_CONCURRENCY": "3",
                "NOTIFICATION_ROUTER_COST_LIMIT_USD": "0.90",
                "NOTIFICATION_ROUTER_PER_CALL_COST_LIMIT_USD": "0.10",
                "NOTIFICATION_ROUTER_API_KEY": "should-not-be-read-or-serialized",
            }
        )
        self.assertFalse(config.api_enabled)
        self.assertEqual(config.extraction_model, "extract-test-v1")
        self.assertEqual(config.routing_model, "route-test-v1")
        self.assertEqual(config.maximum_attempts, 3)
        self.assertEqual(config.concurrency, 3)
        self.assertNotIn("should-not-be-read-or-serialized", json.dumps(config.as_dict()))

    def test_live_provider_requires_explicit_enablement_and_credential(self) -> None:
        disabled = IntegrationConfig(provider_name="http-json", api_enabled=False)
        with self.assertRaises(ProviderConfigurationError):
            build_provider_bundle(disabled, environ={})
        enabled = IntegrationConfig(
            provider_name="http-json",
            api_enabled=True,
            extraction_url="https://example.invalid/extract",
            routing_url="https://example.invalid/route",
        )
        with self.assertRaises(IntegrationConfigError):
            build_provider_bundle(enabled, environ={})

    def test_fake_providers_parse_strict_contracts_without_final_confidence(self) -> None:
        message, packet = self._text_packet()
        del message
        config = IntegrationConfig()
        fake_routing = FakeTextRoutingProvider()
        client = ModelIntegrationClient(
            config,
            extraction_provider=FakeMultimodalProvider(),
            routing_provider=fake_routing,
        )
        result = client.route(packet)
        self.assertEqual(result.value.action, "digest")
        self.assertNotIn("confidence", result.value.as_dict())
        self.assertNotIn("action", json.dumps(packet.as_dict()["message"]))
        self.assertEqual(result.accounting.attempt_count, 1)
        self.assertEqual(fake_routing.calls, 1)

    def test_routing_parser_rejects_extra_confidence_and_bad_evidence(self) -> None:
        _, packet = self._text_packet()
        provider = FakeTextRoutingProvider()
        request = type("Request", (), {"packet": packet, "packet_bytes": packet.prompt_bytes()})()
        raw = provider.route(request, model="fake", timeout_seconds=1).raw_json
        payload = json.loads(raw)
        payload["confidence"] = 0.9
        with self.assertRaises(StructuredOutputError):
            parse_routing_decision(
                json.dumps(payload).encode(),
                allowed_evidence_message_ids=packet.as_dict()["allowed_evidence_message_ids"],
            )
        payload = json.loads(raw)
        payload["selected_evidence_message_ids"] = ["not-allowed"]
        with self.assertRaises(StructuredOutputError):
            parse_routing_decision(
                json.dumps(payload).encode(),
                allowed_evidence_message_ids=packet.as_dict()["allowed_evidence_message_ids"],
            )
        duplicate_key = b'{"action":"digest","action":"mute"}'
        with self.assertRaises(StructuredOutputError):
            parse_routing_decision(duplicate_key)

    def test_reason_bound_is_prompted_and_enforced_with_negative_control(self) -> None:
        _, packet = self._text_packet()
        instructions = packet.prompt_envelope()["instructions"]
        response_contract = instructions["response_contract"]
        self.assertIn(str(MAX_REASON_WORDS), response_contract)
        self.assertIn(str(MAX_REASON_CHARS), response_contract)

        provider = FakeTextRoutingProvider()
        request = type("Request", (), {"packet": packet, "packet_bytes": packet.prompt_bytes()})()
        payload = json.loads(provider.route(request, model="fake", timeout_seconds=1).raw_json)
        payload["reason"] = " ".join(f"word{index}" for index in range(MAX_REASON_WORDS))
        valid = parse_routing_decision(
            json.dumps(payload).encode(),
            allowed_evidence_message_ids=packet.as_dict()["allowed_evidence_message_ids"],
        )
        self.assertEqual(len(valid.reason.split()), MAX_REASON_WORDS)

        payload["reason"] += " overflow"
        with self.assertRaisesRegex(
            StructuredOutputError, "reason exceeds the decision contract bounds"
        ):
            parse_routing_decision(
                json.dumps(payload).encode(),
                allowed_evidence_message_ids=packet.as_dict()["allowed_evidence_message_ids"],
            )

    def test_dataset_local_timestamps_reject_timezone_offsets(self) -> None:
        request = ExtractionRequest(
            media_id="media-time",
            declared_media_type="image",
            declared_path="media/image/time.jpeg",
            detected_format="jpeg",
            content_sha256=hashlib.sha256(b"media-time").hexdigest(),
            media_bytes=b"media-time",
            created_at=datetime(2026, 1, 1, 12, 0),
        )
        extraction = FakeMultimodalProvider().extract(
            request, model="fake", timeout_seconds=1
        )
        extraction_payload = json.loads(extraction.raw_json)
        extraction_payload["created_at"] = "2026-01-01T12:00:00+07:00"
        with self.assertRaisesRegex(
            StructuredOutputError, "created_at must use dataset-local naive time"
        ):
            parse_extraction_record(json.dumps(extraction_payload).encode())

        _, packet = self._text_packet()
        routing = json.loads(
            FakeTextRoutingProvider()
            .route(
                type("Request", (), {"packet": packet, "packet_bytes": packet.prompt_bytes()})(),
                model="fake",
                timeout_seconds=1,
            )
            .raw_json
        )
        routing["semantic_flags"]["time_critical"] = True
        routing["deadline_at"] = "2026-01-01T12:30:00+07:00"
        with self.assertRaisesRegex(
            StructuredOutputError, "deadline_at must use dataset-local naive time"
        ):
            parse_routing_decision(
                json.dumps(routing).encode(),
                allowed_evidence_message_ids=packet.as_dict()["allowed_evidence_message_ids"],
            )

    def test_semantic_support_allows_one_span_and_rejects_duplicate_flag(self) -> None:
        _, packet = self._text_packet()
        schema_description = routing_response_schema()["properties"]["semantic_support"]["description"]
        self.assertIn("false flags must have no support entry", schema_description)
        provider = FakeTextRoutingProvider()
        request = type("Request", (), {"packet": packet, "packet_bytes": packet.prompt_bytes()})()
        payload = json.loads(provider.route(request, model="fake", timeout_seconds=1).raw_json)
        payload["semantic_flags"]["time_critical"] = True
        support = {
            "flag": "time_critical",
            "source_field": "message_text",
            "start_char": 0,
            "end_char_exclusive": 1,
        }
        payload["semantic_support"] = [support]
        valid = parse_routing_decision(
            json.dumps(payload).encode(),
            allowed_evidence_message_ids=packet.as_dict()["allowed_evidence_message_ids"],
        )
        self.assertEqual(len(valid.semantic_support), 1)

        payload["semantic_support"] = [support, dict(support)]
        with self.assertRaisesRegex(
            StructuredOutputError,
            r"semantic_support\[1\]\.flag is invalid or duplicated",
        ):
            parse_routing_decision(
                json.dumps(payload).encode(),
                allowed_evidence_message_ids=packet.as_dict()["allowed_evidence_message_ids"],
            )

    def test_semantic_support_rejects_false_flag_and_accepts_true_flag(self) -> None:
        _, packet = self._text_packet()
        provider = FakeTextRoutingProvider()
        request = type("Request", (), {"packet": packet, "packet_bytes": packet.prompt_bytes()})()
        payload = json.loads(provider.route(request, model="fake", timeout_seconds=1).raw_json)
        support = {
            "flag": "time_critical",
            "source_field": "message_text",
            "start_char": 0,
            "end_char_exclusive": 1,
        }
        payload["semantic_support"] = [support]
        with self.assertRaisesRegex(
            StructuredOutputError, r"semantic_support\[0\] supports a false flag"
        ):
            parse_routing_decision(
                json.dumps(payload).encode(),
                allowed_evidence_message_ids=packet.as_dict()["allowed_evidence_message_ids"],
            )

        payload["semantic_flags"]["time_critical"] = True
        valid = parse_routing_decision(
            json.dumps(payload).encode(),
            allowed_evidence_message_ids=packet.as_dict()["allowed_evidence_message_ids"],
        )
        self.assertEqual(valid.semantic_support[0].flag, "time_critical")

    def test_semantic_support_bounds_are_strict_and_machine_readable(self) -> None:
        _, packet = self._text_packet()
        provider = FakeTextRoutingProvider()
        request = type("Request", (), {"packet": packet, "packet_bytes": packet.prompt_bytes()})()
        payload = json.loads(provider.route(request, model="fake", timeout_seconds=1).raw_json)
        payload["semantic_flags"]["time_critical"] = True
        payload["semantic_support"] = [
            {
                "flag": "time_critical",
                "source_field": "message_text",
                "start_char": 0,
                "end_char_exclusive": 10_000,
            }
        ]
        decision = parse_routing_decision(
            json.dumps(payload).encode(),
            allowed_evidence_message_ids=packet.as_dict()["allowed_evidence_message_ids"],
        )
        with self.assertRaisesRegex(
            StructuredOutputError, "semantic support span exceeds packet field"
        ) as context:
            validate_routing_decision_against_packet(decision, packet.as_dict())
        self.assertEqual(
            context.exception.as_machine_readable(),
            {
                "code": "SCHEMA_INVALID",
                "field": "semantic_support[0]",
                "constraint": "source_field_bounds",
            },
        )

    def test_bounded_schema_retry_uses_machine_feedback_and_preserves_attempts(self) -> None:
        _, packet = self._text_packet()
        feedbacks: list[object] = []
        raw_attempts: list[bytes] = []

        def response_factory(request):
            feedbacks.append(request.validation_feedback)
            payload = json.loads(
                FakeTextRoutingProvider()
                .route(request, model="fake", timeout_seconds=1)
                .raw_json
            )
            payload["semantic_flags"]["time_critical"] = True
            support = {
                "flag": "time_critical",
                "source_field": "message_text",
                "start_char": 0,
                "end_char_exclusive": 1,
            }
            payload["semantic_support"] = [support, dict(support)] if request.validation_feedback is None else [support]
            return payload

        provider = FakeTextRoutingProvider(response_factory=response_factory)
        client = ModelIntegrationClient(
            IntegrationConfig(max_retries=1),
            extraction_provider=FakeMultimodalProvider(),
            routing_provider=provider,
            raw_response_sink=lambda call_id, stage, operation, attempt, metadata, raw: raw_attempts.append(raw),
        )
        result = client.route(packet)

        self.assertEqual(result.accounting.attempt_count, 2)
        self.assertEqual(provider.calls, 2)
        self.assertIs(provider.requests[0].packet, provider.requests[1].packet)
        self.assertIsNone(feedbacks[0])
        self.assertEqual(
            dict(feedbacks[1]),
            {
                "code": "SCHEMA_INVALID",
                "field": "semantic_support[1].flag",
                "constraint": "unique_flag_support",
            },
        )
        self.assertEqual(len(raw_attempts), 2)
        self.assertTrue(all(json.loads(raw) for raw in raw_attempts))

    def test_retry_backoff_is_deterministic_exponential_and_capped(self) -> None:
        _, packet = self._text_packet()
        delays: list[float] = []
        provider = FakeTextRoutingProvider(failures_before_success=2)
        client = ModelIntegrationClient(
            IntegrationConfig(max_retries=2, retry_backoff_seconds=0.25),
            extraction_provider=FakeMultimodalProvider(),
            routing_provider=provider,
            sleeper=delays.append,
        )

        result = client.route(packet)

        self.assertEqual(result.accounting.attempt_count, 3)
        self.assertEqual(provider.calls, 3)
        self.assertEqual(delays, [0.25, 0.5])
        self.assertEqual(client._retry_delay(2), 0.5)
        self.assertEqual(client._retry_delay(8), 30.0)

    def test_retry_budget_rejects_more_than_two_retries(self) -> None:
        with self.assertRaises(IntegrationConfigError):
            IntegrationConfig(max_retries=3)
        with self.assertRaises(IntegrationConfigError):
            IntegrationConfig.from_env({"NOTIFICATION_ROUTER_MAX_RETRIES": "3"})

    def test_extraction_provider_output_is_bound_to_media_request(self) -> None:
        image_message = next(
            row for row in self.harness.router_inputs() if row.media_type == "image"
        )
        metadata = next(row for row in self.tables.images if row.image_id == image_message.media_id)
        sniff = sniff_media_file(
            DATASET,
            media_id=metadata.image_id,
            declared_media_type="image",
            declared_path=metadata.file_path,
        )
        media_bytes = Path(sniff.resolved_path).read_bytes()
        request = ExtractionRequest(
            media_id=image_message.media_id or metadata.image_id,
            declared_media_type="image",
            declared_path=metadata.file_path,
            detected_format=sniff.detected_format,
            content_sha256=hashlib.sha256(media_bytes).hexdigest(),
            media_bytes=media_bytes,
            created_at=image_message.created_at,
        )
        client = ModelIntegrationClient(
            IntegrationConfig(),
            extraction_provider=FakeMultimodalProvider(),
            routing_provider=FakeTextRoutingProvider(),
        )
        extraction = client.extract(request)
        self.assertEqual(extraction.value.media_id, image_message.media_id)
        self.assertEqual(extraction.value.media_state, "ok")
        retrieval = retrieve_history(image_message, self.normalized)
        packet = assemble_routing_packet(
            image_message,
            self.tables,
            self.normalized,
            retrieval,
            media_results=(sniff,),
            extraction_records=(extraction.value,),
        )
        self.assertEqual(packet.as_dict()["media"]["record"]["media_state"], "ok")

    def test_retry_accounting_and_logs_are_redacted(self) -> None:
        _, packet = self._text_packet()
        stream = io.StringIO()
        logger = RedactedCallLogger(stream=stream)
        fake_routing = FakeTextRoutingProvider(failures_before_success=1)
        client = ModelIntegrationClient(
            IntegrationConfig(max_retries=1),
            extraction_provider=FakeMultimodalProvider(),
            routing_provider=fake_routing,
            logger=logger,
        )
        result = client.route(packet)
        self.assertEqual(result.accounting.attempt_count, 2)
        self.assertEqual(fake_routing.calls, 2)
        logger.request_error(
            call_id="test",
            stage="S7",
            operation="route",
            provider="fake",
            model="fake",
            attempt=1,
            code="ROUTER_UNAVAILABLE",
            detail="api_key=super-secret-token password=hunter2",
        )
        log_text = stream.getvalue()
        self.assertNotIn("super-secret-token", log_text)
        self.assertNotIn("hunter2", log_text)
        self.assertNotIn("message_text", log_text)
        self.assertIn("[REDACTED]", log_text)

    def test_cost_budget_blocks_calls_before_provider_invocation(self) -> None:
        _, packet = self._text_packet()
        fake_routing = FakeTextRoutingProvider(cost_usd=0.01)
        client = ModelIntegrationClient(
            IntegrationConfig(
                max_retries=0,
                cost_limit_usd=0.01,
                per_call_cost_limit_usd=0.01,
            ),
            extraction_provider=FakeMultimodalProvider(),
            routing_provider=fake_routing,
        )
        client.route(packet)
        with self.assertRaises(IntegrationError) as context:
            client.route(packet)
        self.assertEqual(context.exception.code, "COST_LIMIT_EXCEEDED")
        self.assertEqual(fake_routing.calls, 1)

    def test_smoke_processes_only_development_modalities(self) -> None:
        config = IntegrationConfig()
        bundle = build_provider_bundle(config)
        result = run_smoke(dataset_dir=DATASET, config=config, bundle=bundle)
        self.assertEqual(result["development_samples_processed"], 3)
        self.assertEqual(result["sample_kinds"], ["text", "image", "voice"])
        development_hashes = {
            safe_identifier(row.message_id) for row in self.harness.router_inputs("development")
        }
        self.assertTrue(
            all(row["message_hash"] in development_hashes for row in result["results"])
        )
        self.assertEqual(result["actual_cost_usd"], 0.0)


if __name__ == "__main__":
    unittest.main()

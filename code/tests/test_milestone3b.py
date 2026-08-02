from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime
from pathlib import Path

from notification_router.config import IntegrationConfig
from notification_router.gemini import _provider_call_error
from notification_router.contracts import (
    extraction_response_schema,
    routing_response_schema,
)
from notification_router.evaluation import EvaluationHarness
from notification_router.integration import IntegrationError, ModelIntegrationClient
from notification_router.packet import RoutingPacket, assemble_routing_packet
from notification_router.providers import (
    ExtractionRequest,
    ProviderConfigurationError,
    RoutingRequest,
    build_provider_bundle,
)
from notification_router.retrieval import retrieve_history
from notification_router.dataset import load_context_dataset, normalize_dataset


ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "dataset"
SAMPLE = DATASET / "sample_messages.csv"


class FakeUsage:
    prompt_token_count = 11
    candidates_token_count = 7
    response_token_count = None
    total_token_count = 18


class FakeResponse:
    def __init__(self, payload: dict[str, object], response_id: str = "fake-response-1") -> None:
        self.text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.response_id = response_id
        self.usage_metadata = FakeUsage()


class FakeHttpOptions:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class FakeGenerateContentConfig:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


class FakePart:
    def __init__(self, *, kind: str, value: object, mime_type: str | None = None) -> None:
        self.kind = kind
        self.value = value
        self.mime_type = mime_type

    @classmethod
    def from_text(cls, *, text: str) -> "FakePart":
        return cls(kind="text", value=text)

    @classmethod
    def from_bytes(cls, *, data: bytes, mime_type: str) -> "FakePart":
        return cls(kind="bytes", value=data, mime_type=mime_type)


class FakeTypes:
    HttpOptions = FakeHttpOptions
    GenerateContentConfig = FakeGenerateContentConfig
    Part = FakePart


class FakeApiError(RuntimeError):
    code = 429


class FakeModels:
    def __init__(
        self,
        *,
        extraction_payload: dict[str, object],
        routing_payload: dict[str, object],
        error: BaseException | None = None,
    ) -> None:
        self.extraction_payload = extraction_payload
        self.routing_payload = routing_payload
        self.error = error
        self.calls: list[dict[str, object]] = []

    def generate_content(self, **kwargs: object) -> FakeResponse:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        config = kwargs["config"]
        schema = config.kwargs["response_json_schema"]
        payload = (
            self.extraction_payload
            if "media_id" in schema["properties"]
            else self.routing_payload
        )
        return FakeResponse(payload, response_id=f"fake-response-{len(self.calls)}")


class FakeClient:
    def __init__(
        self,
        *,
        extraction_payload: dict[str, object],
        routing_payload: dict[str, object],
        error: BaseException | None = None,
    ) -> None:
        self.models = FakeModels(
            extraction_payload=extraction_payload,
            routing_payload=routing_payload,
            error=error,
        )
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class FakeGenAI:
    next_client: FakeClient
    client_calls: list[dict[str, object]] = []

    @classmethod
    def Client(cls, **kwargs: object) -> FakeClient:
        cls.client_calls.append(kwargs)
        return cls.next_client


class MilestoneThreeBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.harness = EvaluationHarness(SAMPLE)
        cls.tables = load_context_dataset(DATASET)
        cls.normalized = normalize_dataset(cls.tables)

    def setUp(self) -> None:
        FakeGenAI.client_calls = []

    def _text_packet(self) -> RoutingPacket:
        message = next(row for row in self.harness.router_inputs() if row.media_type is None)
        retrieval = retrieve_history(message, self.normalized)
        return assemble_routing_packet(
            message,
            self.tables,
            self.normalized,
            retrieval,
            media_results=(),
        )

    @staticmethod
    def _routing_payload() -> dict[str, object]:
        return {
            "action": "digest",
            "message_type": "unknown",
            "reason": "Offline mocked routing proposal.",
            "selected_evidence_message_ids": [],
            "routing_uncertainty": 0.5,
            "uncertainty_reasons": ["mocked_provider"],
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

    @staticmethod
    def _extraction_request(*, detected_format: str = "jpeg") -> ExtractionRequest:
        media_bytes = b"mocked-media-bytes"
        return ExtractionRequest(
            media_id="media-1",
            declared_media_type="image" if detected_format in {"jpeg", "png", "webp", "avif"} else "voice",
            declared_path="media/mock.bin",
            detected_format=detected_format,
            content_sha256=hashlib.sha256(media_bytes).hexdigest(),
            media_bytes=media_bytes,
            created_at=datetime(2026, 1, 1, 12, 0),
        )

    @staticmethod
    def _extraction_payload(request: ExtractionRequest) -> dict[str, object]:
        return {
            "media_id": request.media_id,
            "content_sha256": request.content_sha256,
            "declared_path": request.declared_path,
            "detected_format": request.detected_format,
            "media_state": "ok",
            "extractor_name": "gemini-mock",
            "extractor_version": "gemini-mock-v0",
            "extractor_config_sha256": "0" * 64,
            "extraction_schema_version": "decision-contract-v0.1",
            "extracted_text": "mocked extraction",
            "factual_description": "mocked media description",
            "language": "en",
            "quality_score": 0.8,
            "quality_reasons": ["mocked"],
            "created_at": request.created_at.isoformat(),
        }

    def _fake_sdk(self, request: ExtractionRequest, *, error: BaseException | None = None):
        FakeGenAI.next_client = FakeClient(
            extraction_payload=self._extraction_payload(request),
            routing_payload=self._routing_payload(),
            error=error,
        )
        return (FakeGenAI, FakeTypes)

    def test_vertex_uses_adc_and_structured_schemas_without_network(self) -> None:
        request = self._extraction_request()
        credential_marker = object()
        credential_calls: list[dict[str, object]] = []

        def credentials_loader(**kwargs: object) -> object:
            credential_calls.append(kwargs)
            return credential_marker

        config = IntegrationConfig(
            provider_name="gemini",
            api_enabled=True,
            gemini_backend="vertex",
            gemini_vertex_project="project-test",
            gemini_vertex_location="asia-southeast1",
            gemini_vertex_auth="adc",
            extraction_model="gemini-extraction-test",
            routing_model="gemini-routing-test",
            timeout_seconds=12.5,
            max_retries=0,
        )
        sdk = self._fake_sdk(request)
        bundle = build_provider_bundle(
            config,
            environ={},
            gemini_credentials_loader=credentials_loader,
            gemini_sdk=sdk,
        )
        client = ModelIntegrationClient(
            config,
            extraction_provider=bundle.extraction,
            routing_provider=bundle.routing,
        )

        extraction = client.extract(request)
        routing = client.route(self._text_packet())

        self.assertEqual(extraction.value.media_id, "media-1")
        self.assertEqual(routing.value.action, "digest")
        self.assertEqual(bundle.extraction.name, "gemini-vertex")
        self.assertEqual(len(credential_calls), 1)
        self.assertEqual(credential_calls[0]["auth_mode"], "adc")
        self.assertTrue(FakeGenAI.client_calls[0]["vertexai"])
        self.assertIs(FakeGenAI.client_calls[0]["credentials"], credential_marker)
        self.assertEqual(FakeGenAI.client_calls[0]["project"], "project-test")
        self.assertEqual(FakeGenAI.client_calls[0]["location"], "asia-southeast1")
        self.assertEqual(FakeGenAI.client_calls[0]["http_options"].kwargs["timeout"], 12500)
        self.assertNotIn("api_key", FakeGenAI.client_calls[0])
        sdk_client = FakeGenAI.next_client
        self.assertEqual(len(sdk_client.models.calls), 2)
        extraction_call = sdk_client.models.calls[0]
        extraction_config = extraction_call["config"].kwargs
        self.assertEqual(extraction_config["response_mime_type"], "application/json")
        self.assertEqual(extraction_config["response_json_schema"], extraction_response_schema())
        self.assertEqual(extraction_call["contents"][1].mime_type, "image/jpeg")
        routing_config = sdk_client.models.calls[1]["config"].kwargs
        self.assertEqual(routing_config["response_json_schema"], routing_response_schema())
        self.assertEqual(sdk_client.models.calls[1]["model"], "gemini-routing-test")
        self.assertEqual(extraction.accounting.input_tokens, 11)
        self.assertEqual(routing.accounting.output_tokens, 7)

    def test_ai_studio_uses_only_api_key_and_supports_audio(self) -> None:
        request = self._extraction_request(detected_format="mp3")
        config = IntegrationConfig(
            provider_name="gemini",
            api_enabled=True,
            gemini_backend="ai-studio",
            gemini_api_key_env_var="TEST_GEMINI_KEY",
            extraction_model="gemini-audio-test",
            routing_model="gemini-routing-test",
            max_retries=0,
        )
        sdk = self._fake_sdk(request)
        bundle = build_provider_bundle(
            config,
            environ={
                "TEST_GEMINI_KEY": "mock-key-not-logged",
                "NOTIFICATION_ROUTER_GEMINI_VERTEX_PROJECT": "must-not-be-used",
            },
            gemini_credentials_loader=lambda **_: self.fail("Vertex credentials were accessed"),
            gemini_sdk=sdk,
        )
        client = ModelIntegrationClient(
            config,
            extraction_provider=bundle.extraction,
            routing_provider=bundle.routing,
        )
        result = client.extract(request)

        self.assertEqual(result.value.detected_format, "mp3")
        self.assertEqual(bundle.extraction.name, "gemini-ai-studio")
        self.assertEqual(FakeGenAI.client_calls[0]["api_key"], "mock-key-not-logged")
        self.assertNotIn("credentials", FakeGenAI.client_calls[0])
        self.assertNotIn("project", FakeGenAI.client_calls[0])
        self.assertNotIn("vertexai", FakeGenAI.client_calls[0])
        self.assertEqual(FakeGenAI.next_client.models.calls[0]["contents"][1].mime_type, "audio/mpeg")

    def test_vertex_service_account_mode_uses_only_configured_file(self) -> None:
        request = self._extraction_request()
        credential_marker = object()
        credential_calls: list[dict[str, object]] = []
        config = IntegrationConfig(
            provider_name="gemini",
            api_enabled=True,
            gemini_backend="vertex",
            gemini_vertex_project="project-test",
            gemini_vertex_location="us-central1",
            gemini_vertex_auth="service-account",
            gemini_vertex_credentials_file="C:\\secure\\service-account.json",
        )

        def credentials_loader(**kwargs: object) -> object:
            credential_calls.append(kwargs)
            return credential_marker

        build_provider_bundle(
            config,
            environ={"GOOGLE_APPLICATION_CREDENTIALS": "C:\\unselected\\other.json"},
            gemini_credentials_loader=credentials_loader,
            gemini_sdk=self._fake_sdk(request),
        )

        self.assertEqual(credential_calls[0]["auth_mode"], "service-account")
        self.assertEqual(
            credential_calls[0]["credentials_file"],
            "C:\\secure\\service-account.json",
        )
        self.assertIs(FakeGenAI.client_calls[0]["credentials"], credential_marker)

    def test_gemini_backend_selection_is_explicit_and_api_disabled_by_default(self) -> None:
        disabled = IntegrationConfig(
            provider_name="gemini",
            api_enabled=False,
            gemini_backend="vertex",
        )
        with self.assertRaises(ProviderConfigurationError):
            build_provider_bundle(disabled, gemini_sdk=(object(), FakeTypes))

        missing_backend = IntegrationConfig(provider_name="gemini", api_enabled=True)
        with self.assertRaises(ProviderConfigurationError):
            build_provider_bundle(missing_backend, environ={}, gemini_sdk=(object(), FakeTypes))

        missing_vertex_settings = IntegrationConfig(
            provider_name="gemini",
            api_enabled=True,
            gemini_backend="vertex",
        )
        with self.assertRaises(ProviderConfigurationError):
            build_provider_bundle(
                missing_vertex_settings,
                environ={},
                gemini_sdk=(object(), FakeTypes),
            )

    def test_environment_configuration_is_secret_free(self) -> None:
        config = IntegrationConfig.from_env(
            {
                "NOTIFICATION_ROUTER_PROVIDER": "gemini",
                "NOTIFICATION_ROUTER_API_ENABLED": "1",
                "NOTIFICATION_ROUTER_GEMINI_BACKEND": "vertex",
                "NOTIFICATION_ROUTER_GEMINI_VERTEX_PROJECT": "project-test",
                "NOTIFICATION_ROUTER_GEMINI_VERTEX_LOCATION": "us-central1",
                "NOTIFICATION_ROUTER_GEMINI_VERTEX_AUTH": "service-account",
                "NOTIFICATION_ROUTER_GEMINI_VERTEX_CREDENTIALS_FILE": "C:\\secure\\sa.json",
                "NOTIFICATION_ROUTER_GEMINI_API_KEY_ENV": "MY_GEMINI_KEY",
                "MY_GEMINI_KEY": "must-not-serialize",
                "NOTIFICATION_ROUTER_EXTRACTION_MODEL": "extract-test",
                "NOTIFICATION_ROUTER_ROUTING_MODEL": "route-test",
                "NOTIFICATION_ROUTER_TIMEOUT_SECONDS": "9",
                "NOTIFICATION_ROUTER_MAX_RETRIES": "2",
                "NOTIFICATION_ROUTER_CONCURRENCY": "3",
                "NOTIFICATION_ROUTER_COST_LIMIT_USD": "0.90",
                "NOTIFICATION_ROUTER_PER_CALL_COST_LIMIT_USD": "0.10",
            }
        )
        self.assertEqual(config.gemini_backend, "vertex")
        self.assertEqual(config.gemini_vertex_auth, "service-account")
        self.assertEqual(config.gemini_vertex_project, "project-test")
        self.assertEqual(config.gemini_vertex_location, "us-central1")
        self.assertNotIn("must-not-serialize", json.dumps(config.as_dict()))

    def test_sdk_rate_limit_is_normalized_for_existing_retry_accounting(self) -> None:
        request = self._extraction_request()
        config = IntegrationConfig(
            provider_name="gemini",
            api_enabled=True,
            gemini_backend="ai-studio",
            gemini_api_key_env_var="TEST_GEMINI_KEY",
            max_retries=1,
        )
        sdk = self._fake_sdk(request, error=FakeApiError("secret response must not escape"))
        bundle = build_provider_bundle(
            config,
            environ={"TEST_GEMINI_KEY": "mock-key"},
            gemini_sdk=sdk,
        )
        client = ModelIntegrationClient(
            config,
            extraction_provider=bundle.extraction,
            routing_provider=bundle.routing,
        )

        with self.assertRaises(IntegrationError) as context:
            client.extract(request)
        self.assertEqual(context.exception.code, "PROVIDER_RATE_LIMITED")
        self.assertEqual(context.exception.accounting.attempt_count, 2)
        self.assertNotIn("secret response", str(context.exception))

    def test_sdk_failure_diagnostics_keep_status_and_redact_sensitive_text(self) -> None:
        class FakeNotFoundError(RuntimeError):
            code = 404
            status = "NOT_FOUND"
            message = (
                "Publisher model projects/private-project/locations/private-region/"
                "publishers/google/models/model-x was not found; api_key=secret-value"
            )

        error = _provider_call_error(FakeNotFoundError(), operation="routing")
        self.assertEqual(error.code, "PROVIDER_REQUEST_FAILED")
        self.assertIn("http_status=404", error.detail)
        self.assertIn("reason=NOT_FOUND", error.detail)
        self.assertIn("projects/[REDACTED]", error.detail)
        self.assertNotIn("private-project", error.detail)
        self.assertNotIn("secret-value", error.detail)


if __name__ == "__main__":
    unittest.main()

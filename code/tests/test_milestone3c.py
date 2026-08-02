from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from notification_router.config import IntegrationConfig
from notification_router.dataset import load_context_dataset, normalize_dataset
from notification_router.evaluation import EvaluationHarness
from notification_router.integration import IntegrationError, ModelIntegrationClient
from notification_router.packet import assemble_routing_packet
from notification_router.providers import (
    FakeMultimodalProvider,
    ProviderResponse,
    TokenUsage,
    build_provider_bundle,
)
from notification_router.retrieval import retrieve_history
from notification_router.smoke import _read_env_file, run_smoke


ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "dataset"


class MilestoneThreeCTests(unittest.TestCase):
    def test_env_file_reader_supports_switch_configuration_without_expanding_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# comment\n"
                "export NOTIFICATION_ROUTER_GEMINI_BACKEND=vertex\n"
                "NOTIFICATION_ROUTER_GEMINI_VERTEX_PROJECT=project-test\n"
                "NOTIFICATION_ROUTER_GEMINI_API_KEY='not-read-by-vertex'\n",
                encoding="utf-8",
            )
            values = _read_env_file(path)
        self.assertEqual(values["NOTIFICATION_ROUTER_GEMINI_BACKEND"], "vertex")
        self.assertEqual(values["NOTIFICATION_ROUTER_GEMINI_VERTEX_PROJECT"], "project-test")
        self.assertEqual(values["NOTIFICATION_ROUTER_GEMINI_API_KEY"], "not-read-by-vertex")

    def test_fake_smoke_preserves_raw_responses_under_immutable_artifact_root(self) -> None:
        config = IntegrationConfig()
        bundle = build_provider_bundle(config)
        with tempfile.TemporaryDirectory() as directory:
            result = run_smoke(
                dataset_dir=DATASET,
                config=config,
                bundle=bundle,
                artifact_dir=directory,
            )
            artifact_root = Path(result["artifact_directory"])
            files = sorted(artifact_root.rglob("*.json"))
            raw_files = [path for path in files if "attempt-" in path.name]
            self.assertEqual(result["development_samples_processed"], 3)
            self.assertEqual(len(raw_files), 5)
            self.assertTrue(all(path.is_file() for path in raw_files))
            self.assertTrue(all(json.loads(path.read_text(encoding="utf-8")) for path in raw_files))
            self.assertTrue(
                all(
                    row["routing_packet"]["validated"]
                    and row["routing"]["schema_valid"]
                    and row["routing"]["evidence_within_allowlist"]
                    for row in result["results"]
                )
            )
            self.assertNotIn("messages.csv", " ".join(str(path) for path in files))

    def test_raw_response_sink_writes_before_invalid_output_validation(self) -> None:
        harness = EvaluationHarness(DATASET / "sample_messages.csv")
        tables = load_context_dataset(DATASET)
        normalized = normalize_dataset(tables)
        message = next(row for row in harness.router_inputs("development") if row.media_type is None)
        packet = assemble_routing_packet(
            message,
            tables,
            normalized,
            retrieve_history(message, normalized),
            media_results=(),
        )
        invalid_raw = b'{"unexpected":"shape"}'

        class InvalidRoutingProvider:
            name = "invalid-test"

            def route(self, request, *, model, timeout_seconds):
                del request, model, timeout_seconds
                return ProviderResponse(
                    raw_json=invalid_raw,
                    usage=TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5),
                )

        with tempfile.TemporaryDirectory() as directory:
            persisted: list[Path] = []

            def sink(call_id, stage, operation, attempt, metadata, raw_response):
                del call_id, stage, metadata
                path = Path(directory) / f"{operation}-{attempt}.json"
                path.write_bytes(raw_response)
                persisted.append(path)

            client = ModelIntegrationClient(
                IntegrationConfig(max_retries=0),
                extraction_provider=FakeMultimodalProvider(),
                routing_provider=InvalidRoutingProvider(),
                raw_response_sink=sink,
            )
            with self.assertRaises(IntegrationError) as context:
                client.route(packet)
            self.assertEqual(context.exception.code, "SCHEMA_INVALID")
            self.assertEqual(len(persisted), 1)
            self.assertEqual(persisted[0].read_bytes(), invalid_raw)


if __name__ == "__main__":
    unittest.main()

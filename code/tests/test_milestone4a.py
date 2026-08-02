import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

import notification_router.dataset as dataset_module
from notification_router.artifacts import build_label_free_run_manifest
from notification_router.baseline import (
    BaselineRunnerConfig,
    run_development_baseline,
)
from notification_router.confidence import calculate_final_confidence
from notification_router.config import IntegrationConfig
from notification_router.contracts import ExtractionRecord, RawRoutingDecision
from notification_router.dataset import load_context_dataset, normalize_dataset
from notification_router.evaluation import EvaluationHarness
from notification_router.extraction_cache import (
    ExtractionCache,
    build_extraction_cache_identity,
)
from notification_router.features import compute_deterministic_features
from notification_router.finalization import validate_routing_safety
from notification_router.packet import assemble_routing_packet
from notification_router.providers import (
    ExtractionRequest,
    FakeMultimodalProvider,
    FakeTextRoutingProvider,
    ProviderBundle,
)
from notification_router.retrieval import retrieve_history
from notification_router.schemas import ACTION_VALUES


ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "dataset"
SAMPLE = DATASET / "sample_messages.csv"


class MilestoneFourATests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.harness = EvaluationHarness(SAMPLE)
        cls.tables = load_context_dataset(DATASET)
        cls.normalized = normalize_dataset(cls.tables)

    def _text_context(self):
        message = next(row for row in self.harness.router_inputs() if row.media_type is None)
        retrieval = retrieve_history(message, self.normalized)
        features, constraints = compute_deterministic_features(
            message, self.tables, self.normalized
        )
        packet = assemble_routing_packet(
            message,
            self.tables,
            self.normalized,
            retrieval,
            media_results=(),
            deterministic_features=features.as_dict(),
            safety_constraints=constraints.as_dict(),
        )
        provider = FakeTextRoutingProvider()
        request = type("Request", (), {"packet": packet, "packet_bytes": packet.prompt_bytes()})()
        raw = provider.route(request, model="fake", timeout_seconds=1).raw_json
        payload = json.loads(raw)
        decision = RawRoutingDecision(
            action=payload["action"],
            message_type=payload["message_type"],
            reason=payload["reason"],
            selected_evidence_message_ids=tuple(payload["selected_evidence_message_ids"]),
            routing_uncertainty=payload["routing_uncertainty"],
            uncertainty_reasons=tuple(payload["uncertainty_reasons"]),
            semantic_flags=MappingProxyType(payload["semantic_flags"]),
            deadline_at=None,
            semantic_support=(),
            reported_contradictory_signal_count=0,
        )
        return message, retrieval, features, packet, decision

    def test_confidence_is_monotonic_and_audited(self) -> None:
        message, retrieval, features, packet, decision = self._text_context()
        lower_uncertainty = calculate_final_confidence(
            message=message,
            packet=packet,
            decision=decision,
            features=features,
            extraction_record=None,
            retrieval=retrieval,
            routing_attempt_count=1,
        )
        higher_uncertainty = calculate_final_confidence(
            message=message,
            packet=packet,
            decision=replace(decision, routing_uncertainty=0.9),
            features=features,
            extraction_record=None,
            retrieval=retrieval,
            routing_attempt_count=1,
        )
        self.assertGreaterEqual(
            lower_uncertainty.rounded_confidence,
            higher_uncertainty.rounded_confidence,
        )
        self.assertIn("confidence_policy_version", lower_uncertainty.as_dict())
        self.assertIn("aggregate_snapshot_undefended", lower_uncertainty.penalties)

    def test_quiet_hours_constraint_is_strict_and_trusted_flags_do_not_force_mute(self) -> None:
        message, retrieval, features, packet, decision = self._text_context()
        quiet_features = replace(features, quiet_hours=True)
        constrained_payload = packet.as_dict()
        constrained_payload["safety_constraints"] = {
            "allowed_actions": list(ACTION_VALUES),
            "required_action": None,
            "prohibited_actions": ["notify"],
            "triggered_invariants": ["INV-103"],
        }
        constrained_packet = type(packet)(MappingProxyType(constrained_payload))
        with self.assertRaisesRegex(Exception, "quiet hours"):
            validate_routing_safety(
                message=message,
                packet=constrained_packet,
                decision=replace(decision, action="notify"),
                features=quiet_features,
                retrieval=retrieval,
            )
        trusted_features = replace(
            features,
            quiet_hours=False,
            trusted_business=True,
            domain_mismatch=False,
        )
        audit = validate_routing_safety(
            message=message,
            packet=packet,
            decision=decision,
            features=trusted_features,
            retrieval=retrieval,
        )
        self.assertFalse(audit.high_risk_required_mute)

    def test_extraction_cache_uses_content_and_configuration_identity(self) -> None:
        content = b"ID3 synthetic audio bytes"
        content_hash = hashlib.sha256(content).hexdigest()
        request = ExtractionRequest(
            media_id="media-a",
            declared_media_type="voice",
            declared_path="media/audio/a.mp3",
            detected_format="mp3",
            content_sha256=content_hash,
            media_bytes=content,
            created_at=datetime(2026, 8, 1, 12, 0),
        )
        identity = build_extraction_cache_identity(
            request,
            provider_name="gemini-vertex",
            model_name="model-a",
            backend="vertex",
            timeout_seconds=30,
        )
        self.assertIsNotNone(identity)
        record = ExtractionRecord(
            media_id="media-a",
            content_sha256=content_hash,
            declared_path="media/audio/a.mp3",
            detected_format="mp3",
            media_state="ok",
            extractor_name="gemini-vertex",
            extractor_version="v0",
            extractor_config_sha256=hashlib.sha256(b"config").hexdigest(),
            extraction_schema_version="extraction-record-v0",
            extracted_text="synthetic extraction",
            factual_description="synthetic description",
            language="und",
            quality_score=0.9,
            quality_reasons=("synthetic",),
            created_at=request.created_at,
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = ExtractionCache(directory)
            cache.put(identity, record, (b"{\"synthetic\":true}",))
            lookup = cache.lookup(identity)
            self.assertTrue(lookup.hit)
            self.assertEqual(lookup.value.record.extracted_text, "synthetic extraction")
            different_path = replace(request, media_id="media-b", declared_path="other.mp3")
            same_identity = build_extraction_cache_identity(
                different_path,
                provider_name="gemini-vertex",
                model_name="model-a",
                backend="vertex",
                timeout_seconds=30,
            )
            self.assertEqual(identity.key, same_identity.key)
            changed_content = replace(
                request,
                content_sha256=hashlib.sha256(b"changed").hexdigest(),
                media_bytes=b"changed",
            )
            changed_identity = build_extraction_cache_identity(
                changed_content,
                provider_name="gemini-vertex",
                model_name="model-a",
                backend="vertex",
                timeout_seconds=30,
            )
            self.assertNotEqual(identity.key, changed_identity.key)

    def test_explicit_baseline_run_nonce_changes_identity(self) -> None:
        common = {
            "partition": "development",
            "source_file_sha256": "source",
            "sanitized_input_sha256": "inputs",
            "split_manifest_sha256": "split",
            "row_count": 20,
            "configuration": {"runner": "test"},
        }
        deterministic = build_label_free_run_manifest(**common)
        fresh = build_label_free_run_manifest(**common, run_nonce="fresh-20260802-01")
        self.assertNotEqual(deterministic.run_id, fresh.run_id)
        self.assertEqual(fresh.as_dict()["run_nonce"], "fresh-20260802-01")

    def test_baseline_run_nonce_preserves_write_once_and_separates_runs(self) -> None:
        config = IntegrationConfig()
        with tempfile.TemporaryDirectory() as artifact_directory, tempfile.TemporaryDirectory() as cache_directory:
            first = run_development_baseline(
                dataset_dir=DATASET,
                config=config,
                bundle=ProviderBundle(
                    extraction=FakeMultimodalProvider(),
                    routing=FakeTextRoutingProvider(),
                ),
                runner_config=BaselineRunnerConfig(
                    artifact_root=Path(artifact_directory),
                    cache_root=Path(cache_directory),
                    run_nonce="first",
                ),
            )
            second = run_development_baseline(
                dataset_dir=DATASET,
                config=config,
                bundle=ProviderBundle(
                    extraction=FakeMultimodalProvider(),
                    routing=FakeTextRoutingProvider(),
                ),
                runner_config=BaselineRunnerConfig(
                    artifact_root=Path(artifact_directory),
                    cache_root=Path(cache_directory),
                    run_nonce="second",
                ),
            )
            self.assertNotEqual(first.manifest.run_id, second.manifest.run_id)
            self.assertTrue((first.artifact_directory / "manifest.json").is_file())
            self.assertTrue((second.artifact_directory / "manifest.json").is_file())

    def test_fake_baseline_is_label_isolated_and_writes_immutable_bundle(self) -> None:
        opened: list[str] = []
        original_loader = dataset_module.load_csv_table

        def tracking_loader(path, table_name=None):
            opened.append(str(table_name or Path(path).name))
            return original_loader(path, table_name)

        config = IntegrationConfig()
        with tempfile.TemporaryDirectory() as artifact_directory, tempfile.TemporaryDirectory() as cache_directory:
            with patch.object(dataset_module, "load_csv_table", side_effect=tracking_loader):
                result = run_development_baseline(
                    dataset_dir=DATASET,
                    config=config,
                    bundle=ProviderBundle(
                        extraction=FakeMultimodalProvider(),
                        routing=FakeTextRoutingProvider(),
                    ),
                    runner_config=BaselineRunnerConfig(
                        artifact_root=Path(artifact_directory),
                        cache_root=Path(cache_directory),
                        total_cost_limit_usd=0.50,
                    ),
                )
            self.assertFalse(result.aborted)
            self.assertEqual(result.completed_rows, 20)
            self.assertEqual(result.failed_rows, 0)
            self.assertEqual(result.degraded_rows, 0)
            self.assertNotIn("messages.csv", opened)
            self.assertEqual(result.metrics["baseline"]["completed_rows"], 20)
            extraction_cache = result.metrics["baseline"]["extraction_cache"]
            self.assertEqual(extraction_cache["hits"] + extraction_cache["misses"], 7)
            self.assertGreaterEqual(extraction_cache["hits"], 1)
            self.assertTrue((result.artifact_directory / "manifest.json").is_file())
            self.assertTrue((result.artifact_directory / "raw_predictions.jsonl").is_file())
            self.assertTrue((result.artifact_directory / "metrics.json").is_file())
            self.assertTrue((result.artifact_directory / "errors.jsonl").is_file())
            row_records = list((result.artifact_directory / "rows").rglob("record.json"))
            self.assertEqual(len(row_records), 20)
            manifest_text = (result.artifact_directory / "manifest.json").read_text(encoding="utf-8")
            self.assertNotIn("sealed_holdout", manifest_text)


if __name__ == "__main__":
    unittest.main()

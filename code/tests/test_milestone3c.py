from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from notification_router.config import IntegrationConfig
from notification_router.providers import build_provider_bundle
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


if __name__ == "__main__":
    unittest.main()

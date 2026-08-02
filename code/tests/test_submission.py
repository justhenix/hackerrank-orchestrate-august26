import tempfile
import unittest
from pathlib import Path

from notification_router.predictions import RawPrediction
from notification_router.submission import (
    SubmissionValidationError,
    validate_output_csv,
    validate_predictions,
    write_output_csv,
)


class SubmissionTests(unittest.TestCase):
    def _predictions(self):
        return (
            RawPrediction(
                message_id="m1",
                action="notify",
                message_type="urgent",
                reason="deadline requires attention",
                selected_evidence_message_ids=("h1",),
                confidence=0.8,
            ),
            RawPrediction(
                message_id="m2",
                action="digest",
                message_type="personal",
                reason="routine message",
                selected_evidence_message_ids=(),
                confidence=0.2,
            ),
        )

    def test_exact_output_is_atomic_and_reparseable(self) -> None:
        expected = ("m1", "m2")
        allowlists = {"m1": ("h1",), "m2": ()}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.csv"
            artifact = write_output_csv(
                output,
                self._predictions(),
                expected_ids=expected,
                evidence_allowlists=allowlists,
            )
            self.assertEqual(artifact.row_count, 2)
            self.assertFalse((Path(directory) / ".output.csv.tmp").exists())
            reparsed = validate_output_csv(
                output,
                expected_ids=expected,
                evidence_allowlists=allowlists,
            )
            self.assertEqual(reparsed.sha256, artifact.sha256)
            text = output.read_text(encoding="utf-8")
            self.assertIn("evidence_message_ids\n", text)
            self.assertIn(",none\n", text)

    def test_prediction_validation_rejects_id_set_and_evidence_failures(self) -> None:
        invalid = self._predictions() + (
            RawPrediction(
                message_id="unexpected",
                action="mute",
                message_type="spam",
                reason="unexpected row",
                selected_evidence_message_ids=("foreign",),
                confidence=0.4,
            ),
        )
        with self.assertRaises(SubmissionValidationError) as context:
            validate_predictions(
                invalid,
                expected_ids=("m1", "m2"),
                evidence_allowlists={"m1": ("h1",), "m2": ()},
            )
        self.assertIn("row count mismatch", str(context.exception))
        self.assertIn("unexpected message IDs", str(context.exception))

    def test_prediction_validation_rejects_invalid_contract_fields(self) -> None:
        invalid = (
            RawPrediction(
                message_id="m1",
                action="notify",
                message_type="not-a-contract-type",
                reason="reason",
                selected_evidence_message_ids=("h1", "h1"),
                confidence=1.1,
            ),
        )
        with self.assertRaises(SubmissionValidationError) as context:
            validate_predictions(
                invalid,
                expected_ids=("m1",),
                evidence_allowlists={"m1": ("h1",)},
            )
        message = str(context.exception)
        self.assertIn("message_type", message)
        self.assertIn("duplicate_selected_evidence_message_ids", message)
        self.assertIn("confidence", message)


if __name__ == "__main__":
    unittest.main()

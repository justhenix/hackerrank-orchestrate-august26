"""Bounded target execution and exact output generation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Mapping, Sequence

from .artifacts import ImmutableArtifactStore
from .baseline import (
    BaselineConfigurationError,
    BaselineRunnerConfig,
    _load_vertex_configuration,
    run_target_baseline,
)
from .config import IntegrationConfigError
from .dataset import load_dataset
from .submission import SubmissionArtifact, validate_output_csv, write_output_csv


def run_target_submission(
    *,
    dataset_dir: str | Path,
    output_path: str | Path,
    config,
    runner_config: BaselineRunnerConfig,
    bundle=None,
    environment: Mapping[str, str] | None = None,
    log_file: str | Path | None = None,
) -> tuple[object, SubmissionArtifact]:
    """Run target rows and write output only after full contract validation."""

    target_tables = load_dataset(dataset_dir)
    expected_ids = tuple(message.message_id for message in target_tables.messages)
    result = run_target_baseline(
        dataset_dir=dataset_dir,
        config=config,
        runner_config=runner_config,
        bundle=bundle,
        environment=environment,
        log_file=log_file,
    )
    if result.aborted:
        raise BaselineConfigurationError("target baseline aborted before output generation")
    artifact = write_output_csv(
        output_path,
        result.predictions,
        expected_ids=expected_ids,
        evidence_allowlists=result.evidence_allowlists,
    )
    validated = validate_output_csv(
        artifact.path,
        expected_ids=expected_ids,
        evidence_allowlists=result.evidence_allowlists,
    )
    if validated.sha256 != artifact.sha256 or validated.row_count != len(expected_ids):
        raise BaselineConfigurationError("output changed during post-write validation")
    store = ImmutableArtifactStore(result.artifact_directory)
    store.write_json(
        "submission.json",
        {
            "output": validated.as_dict(),
            "exact_target_row_count": len(expected_ids),
            "atomic_write": True,
            "labels_visible_to_router": False,
        },
    )
    return result, validated


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, default=Path("../dataset"))
    parser.add_argument("--output", type=Path, default=Path("../dataset/output.csv"))
    parser.add_argument("--env-file", type=Path, default=Path("../.env"))
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path("../.artifacts/target"),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("../.artifacts/target/cache"),
    )
    parser.add_argument("--max-cost-usd", type=float, default=1.0)
    parser.add_argument("--run-id", dest="run_nonce")
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config, environment = _load_vertex_configuration(args.env_file, args.max_cost_usd)
        runner_config = BaselineRunnerConfig(
            artifact_root=args.artifact_dir,
            cache_root=args.cache_dir,
            total_cost_limit_usd=args.max_cost_usd,
            run_nonce=args.run_nonce,
        )
        result, output = run_target_submission(
            dataset_dir=args.dataset_dir,
            output_path=args.output,
            config=config,
            runner_config=runner_config,
            environment=environment,
            log_file=args.log_file,
        )
    except (BaselineConfigurationError, IntegrationConfigError) as exc:
        print(f"target failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    report = {
        "run": result.as_dict(),
        "output": output.as_dict(),
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

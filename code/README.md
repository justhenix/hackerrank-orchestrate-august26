# Architecture v0.1: Milestone 1 frozen, Milestone 2 in progress

This directory is the complete submission package for the deterministic
Architecture v0.1 implementation. Milestone 1 is frozen. Milestone 2 adds
label-isolated evaluation, deterministic historical retrieval, evidence
allowlisting, and model-free routing-packet assembly.

Implemented scope:

- exact UTF-8 CSV loading and typed schema validation;
- primary/composite key and referential-integrity checks;
- normalized nullable joins;
- same-user, strictly-prior history filtering;
- bounded-header byte-signature media sniffing;
- a read-only diagnostic CLI and standard-library tests.

Milestone 2 additionally includes:

- sanitized sample inputs with deterministic 20-row development and 10-row
  sealed holdout partitions;
- write-once run manifests and raw prediction artifacts;
- schema, action, message-type, evidence, raw-confidence, latency, and cost
  metrics;
- deterministic same-user, strictly-prior, non-embedding retrieval with
  stable top-K ordering and a validated evidence allowlist;
- canonical, label-free routing packets with prompt/data separation.

No model/API calls, OCR/ASR extraction, embeddings, routing decisions, final
confidence calculation, caching, or UI logic is included. The diagnostic
command never writes `output.csv`.

## Timestamp policy

CSV timestamps are dataset-local naive wall-clock values. The runtime does not
infer a geographic timezone, attach an offset, or convert source timestamps.
All ordering and strictly-prior comparisons use the source wall-clock values
directly. Quiet-hour calculations in later milestones must use the same
dataset-local wall-clock convention.

## Install

Open a terminal in this `code/` directory. Python 3.11 or 3.12 is supported.

```text
python -m pip install -r requirements.txt
python -m pip install -e .
```

Milestones 1 and 2 have no third-party runtime dependencies. `requirements.txt`
installs only the package build tool; the editable installation exposes the
diagnostic `notification-router` command.

## Milestone 2 evaluation boundary

`EvaluationHarness.router_inputs()` is the router-facing boundary. It exposes
only the eleven input columns; expected action, type, reason, confidence, and
evidence labels remain evaluator-side. Holdout rows require an explicit
evaluator reveal and are not returned by default.

The harness API is available from `notification_router.evaluation`, retrieval
from `notification_router.retrieval`, and packet assembly from
`notification_router.packet`. No model invocation is performed by these APIs.

## Deferred decisions

- Model/provider selection, OCR/ASR behavior, extraction caching, and routing
  are deferred until a later milestone.
- Confidence is preserved and measured as a raw proposal; deterministic final
  confidence and calibration policy are deferred.
- Latency and cost metrics use caller-supplied values; no provider accounting
  or token-cost policy exists yet.
- Evidence scoring reports exact sample-ID overlap. Semantic relevance and
  alternate-valid-evidence equivalence remain evaluator decisions.

## Run diagnostics

Pass the participant dataset directory explicitly. From this directory in the
starter repository:

```text
python main.py --dataset-dir ../dataset
```

Equivalent installed command:

```text
notification-router --dataset-dir ../dataset
```

Use `--json` for deterministic machine-readable output:

```text
python main.py --dataset-dir ../dataset --json
```

## Run tests

From this `code/` directory:

```text
python -m unittest discover -s tests -v
```

The integration tests expect the participant dataset at `../dataset`, matching
the challenge repository layout. Runtime operation accepts any dataset path
through `--dataset-dir`; the dataset itself is not part of the submitted code
package.

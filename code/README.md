# Architecture v0.1: Milestones 1–2 frozen, Milestone 3A plumbing

This directory is the complete submission package for the deterministic
Architecture v0.1 implementation. Milestones 1 and 2 are frozen. Milestone 3A
adds provider-neutral model integration plumbing without changing the frozen
stage boundaries or final-decision responsibilities.

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

Milestone 3A additionally includes:

- separate multimodal extraction and text-routing provider protocols;
- strict extraction and raw-routing-decision parsing against the decision
  contracts, with final confidence intentionally absent;
- environment-only provider configuration, bounded retries/concurrency/timeouts,
  cumulative and per-call cost limits, and opt-in API access;
- fake providers, redacted JSONL call telemetry, and per-attempt
  latency/token/cost accounting;
- an optional smoke runner limited to one development text, image, and voice
  sample, using context files without opening `messages.csv`.

No API call occurs unless explicitly enabled. Milestone 3A does not implement
prompt tuning, final confidence calculation or calibration, UI, target output
generation, embeddings, caching, OCR/ASR implementations, or routing rules.
The diagnostic and smoke commands never write `output.csv`.

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

Milestones 1–3A have no third-party runtime dependencies. `requirements.txt`
installs only the package build tool; the editable installation exposes the
diagnostic `notification-router` and bounded `notification-router-smoke`
commands.

## Milestone 3A configuration

API calls are disabled by default. Fake providers require no credentials or
internet access:

```text
python -m notification_router.smoke --dataset-dir ../dataset --json
```

The provider-neutral HTTP gateway requires an explicit enable flag and these
environment variables: `NOTIFICATION_ROUTER_API_ENABLED=1`,
`NOTIFICATION_ROUTER_PROVIDER=http-json`, `NOTIFICATION_ROUTER_API_KEY`,
`NOTIFICATION_ROUTER_EXTRACTION_URL`, and
`NOTIFICATION_ROUTER_ROUTING_URL`. Model names, timeout, retry count,
concurrency, and cost ceilings are configured through the corresponding
`NOTIFICATION_ROUTER_*` variables in `notification_router.config`.

Later real smoke-test command (PowerShell; replace endpoint values):

```powershell
$env:NOTIFICATION_ROUTER_API_ENABLED="1"; $env:NOTIFICATION_ROUTER_PROVIDER="http-json"; $env:NOTIFICATION_ROUTER_API_KEY="<set-secret>"; $env:NOTIFICATION_ROUTER_EXTRACTION_URL="https://provider.example/extract"; $env:NOTIFICATION_ROUTER_ROUTING_URL="https://provider.example/route"; python -m notification_router.smoke --dataset-dir ../dataset --provider http-json --enable-api --max-cost-usd 0.60 --json
```

The gateway expects JSON envelopes containing `output` and optional `usage`
fields; the provider and endpoint schema remain intentionally unresolved.
The default six logical calls, with one retry per call and a `$0.05` per-call
ceiling, have a configured maximum smoke budget of `$0.60`.

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
  semantics remain provider decisions; 3A supplies only a generic HTTP
  adapter and offline fakes.
- Confidence is preserved and measured as a raw proposal; deterministic final
  confidence and calibration policy are deferred.
- Latency, token usage, and cost are now accounted per provider attempt; live
  pricing remains caller-configured and unverified.
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

# Architecture v0.1: Milestones 1-2 frozen, Milestones 3A-3B plumbing

This directory is the complete submission package for the deterministic
Architecture v0.1 implementation. Milestones 1 and 2 are frozen. Milestones
3A and 3B add provider-neutral and Google Gemini model integration plumbing
without changing the frozen stage boundaries or final-decision responsibilities.

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

Milestone 3B additionally includes:

- explicit Google Gemini adapters using the official `google-genai` SDK;
- Vertex AI with explicit project, location, and ADC or service-account
  authentication;
- Gemini Developer API / AI Studio with explicit API-key authentication;
- schema-constrained text routing and image/audio extraction for both backends;
- fully mocked backend tests that make zero network calls.

No API call occurs unless explicitly enabled. Milestones 3A-3B do not implement
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

Milestones 1-3A use only the Python standard library. Milestone 3B adds the
official `google-genai` SDK for explicitly enabled live adapters. The SDK is
imported lazily, so fake-provider tests still run without credentials or
network access. The editable installation exposes the diagnostic
`notification-router` and bounded `notification-router-smoke` commands.

## Milestone 3A-3B configuration

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

The Google Gemini adapter requires these common variables:

```text
NOTIFICATION_ROUTER_API_ENABLED=1
NOTIFICATION_ROUTER_PROVIDER=gemini
NOTIFICATION_ROUTER_GEMINI_BACKEND=vertex | ai-studio
NOTIFICATION_ROUTER_EXTRACTION_MODEL=<explicit-model-name>
NOTIFICATION_ROUTER_ROUTING_MODEL=<explicit-model-name>
```

Timeout, retries, concurrency, total/per-call cost ceilings, and optional
input/output token prices use the existing `NOTIFICATION_ROUTER_*` variables.
`NOTIFICATION_ROUTER_GEMINI_BACKEND` is mandatory for Gemini; there is no
automatic backend fallback.

Vertex AI uses these variables:

```text
NOTIFICATION_ROUTER_GEMINI_VERTEX_PROJECT=<google-cloud-project>
NOTIFICATION_ROUTER_GEMINI_VERTEX_LOCATION=<region>
NOTIFICATION_ROUTER_GEMINI_VERTEX_AUTH=adc | service-account
```

The standard `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` names are
accepted as equivalent project/location inputs.

`adc` uses Google Application Default Credentials, including the standard
`GOOGLE_APPLICATION_CREDENTIALS` path when configured. For explicit
service-account loading, set
`NOTIFICATION_ROUTER_GEMINI_VERTEX_CREDENTIALS_FILE` to a local JSON path.
Never commit that file or place its contents in environment-controlled source.

Gemini Developer API / AI Studio uses an API key from
`NOTIFICATION_ROUTER_GEMINI_API_KEY` by default. To use a differently named
secret environment variable, set `NOTIFICATION_ROUTER_GEMINI_API_KEY_ENV`.
Only the credential for the selected backend is read.

Later real smoke-test command (PowerShell; replace endpoint values):

```powershell
$env:NOTIFICATION_ROUTER_API_ENABLED="1"; $env:NOTIFICATION_ROUTER_PROVIDER="http-json"; $env:NOTIFICATION_ROUTER_API_KEY="<set-secret>"; $env:NOTIFICATION_ROUTER_EXTRACTION_URL="https://provider.example/extract"; $env:NOTIFICATION_ROUTER_ROUTING_URL="https://provider.example/route"; python -m notification_router.smoke --dataset-dir ../dataset --provider http-json --enable-api --max-cost-usd 0.60 --json
```

The gateway expects JSON envelopes containing `output` and optional `usage`
fields; the provider and endpoint schema remain intentionally unresolved.
The default six logical calls, with one retry per call and a `$0.05` per-call
ceiling, have a configured maximum smoke budget of `$0.60`.

Later real Vertex AI smoke test (PowerShell; ADC; replace model/project values):

```powershell
$env:NOTIFICATION_ROUTER_API_ENABLED="1"
$env:NOTIFICATION_ROUTER_PROVIDER="gemini"
$env:NOTIFICATION_ROUTER_GEMINI_BACKEND="vertex"
$env:NOTIFICATION_ROUTER_GEMINI_VERTEX_PROJECT="<project>"
$env:NOTIFICATION_ROUTER_GEMINI_VERTEX_LOCATION="<location>"
$env:NOTIFICATION_ROUTER_GEMINI_VERTEX_AUTH="adc"
$env:NOTIFICATION_ROUTER_EXTRACTION_MODEL="<multimodal-model>"
$env:NOTIFICATION_ROUTER_ROUTING_MODEL="<routing-model>"
python -m notification_router.smoke --dataset-dir ../dataset --provider gemini --enable-api --max-cost-usd 0.60 --json
```

Later real AI Studio smoke test (PowerShell; replace key/model values):

```powershell
$env:NOTIFICATION_ROUTER_API_ENABLED="1"
$env:NOTIFICATION_ROUTER_PROVIDER="gemini"
$env:NOTIFICATION_ROUTER_GEMINI_BACKEND="ai-studio"
$env:NOTIFICATION_ROUTER_GEMINI_API_KEY="<set-secret>"
$env:NOTIFICATION_ROUTER_EXTRACTION_MODEL="<multimodal-model>"
$env:NOTIFICATION_ROUTER_ROUTING_MODEL="<routing-model>"
python -m notification_router.smoke --dataset-dir ../dataset --provider gemini --enable-api --max-cost-usd 0.60 --json
```

Both commands remain bounded to one development text, image, and voice
sample. They never access the sealed holdout or `messages.csv`, and they do
not write `output.csv`.

## Milestone 2 evaluation boundary

`EvaluationHarness.router_inputs()` is the router-facing boundary. It exposes
only the eleven input columns; expected action, type, reason, confidence, and
evidence labels remain evaluator-side. Holdout rows require an explicit
evaluator reveal and are not returned by default.

The harness API is available from `notification_router.evaluation`, retrieval
from `notification_router.retrieval`, and packet assembly from
`notification_router.packet`. No model invocation is performed by these APIs.

## Deferred decisions

- OCR/ASR behavior, extraction caching, and routing semantics remain provider
  decisions; 3B supplies bounded Gemini adapters, a generic HTTP adapter, and
  offline fakes without selecting production prompts or policies.
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

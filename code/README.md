# Architecture v0.1: Milestone 4A development baseline

This directory is the complete submission package for the deterministic
Architecture v0.1 implementation. Milestones 1 and 2 are frozen. Milestones
3A and 3B add provider-neutral and Google Gemini model integration plumbing.
Milestone 4A connects those stages for one immutable 20-row development
baseline without opening the sealed holdout or target messages.

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

Milestone 4A additionally includes:

- a label-isolated development runner over exactly 20 sanitized rows;
- deterministic S4 features, initial safety constraints, strict S8 validation,
  and the Architecture v0.1 final-confidence policy;
- immutable per-row packet hashes, raw attempts, extraction records, final
  decisions, error records, and evaluator metrics;
- content-addressed successful extraction caching keyed by media content,
  detected format, model, extractor/schema, and configuration identity.

No API call occurs unless explicitly enabled. The baseline CLI requires an
explicit Vertex AI configuration and never writes `output.csv`. It does not
read `dataset/messages.csv` for prediction or reveal the sealed holdout.

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
network access. The editable installation exposes the diagnostic,
bounded-smoke, and development-baseline commands.

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
python -m notification_router.smoke --dataset-dir ../dataset --provider gemini --enable-api --env-file ../.env --artifact-dir ../.artifacts/milestone3c --max-cost-usd 0.10 --json
```

Later real AI Studio smoke test (PowerShell; replace key/model values):

```powershell
$env:NOTIFICATION_ROUTER_API_ENABLED="1"
$env:NOTIFICATION_ROUTER_PROVIDER="gemini"
$env:NOTIFICATION_ROUTER_GEMINI_BACKEND="ai-studio"
$env:NOTIFICATION_ROUTER_GEMINI_API_KEY="<set-secret>"
$env:NOTIFICATION_ROUTER_EXTRACTION_MODEL="<multimodal-model>"
$env:NOTIFICATION_ROUTER_ROUTING_MODEL="<routing-model>"
python -m notification_router.smoke --dataset-dir ../dataset --provider gemini --enable-api --env-file ../.env --artifact-dir ../.artifacts/milestone3c --max-cost-usd 0.10 --json
```

Both commands remain bounded to one development text, image, and voice
sample. They never access the sealed holdout or `messages.csv`, and they do
not write `output.csv`. Pass `--env-file ../.env` to load a local switchable
configuration explicitly. Pass `--artifact-dir ../.artifacts/milestone3c` to
preserve write-once raw response bytes and bounded smoke metadata; `.env` and
`.artifacts/` are local-only and ignored.

## Milestone 4A development baseline

Run the complete Vertex AI development baseline from this `code/` directory:

```powershell
python -m notification_router.baseline --dataset-dir ../dataset --env-file ../.env --artifact-dir ../.artifacts/milestone4a --cache-dir ../.artifacts/milestone4a/cache --max-cost-usd 1.00 --json
```

For an explicit fresh rerun, provide a new safe run namespace and a clean
artifact directory; the previous run remains write-once and untouched:

```powershell
python -m notification_router.baseline --dataset-dir ../dataset --env-file ../.env --artifact-dir ../.artifacts/milestone4a-fresh --cache-dir ../.artifacts/milestone4a-fresh/cache --run-id 20260802T-baseline-01 --max-cost-usd 1.00 --json
```

The command requires `NOTIFICATION_ROUTER_API_ENABLED=1`,
`NOTIFICATION_ROUTER_PROVIDER=gemini`, and
`NOTIFICATION_ROUTER_GEMINI_BACKEND=vertex` in the loaded environment. It
processes only the 20-row development split from `sample_messages.csv`, uses
ADC/service-account configuration already described above, and writes only
under the ignored `.artifacts/milestone4a/` directory. A second run with a
different artifact directory can reuse successful content-addressed extraction
entries; completed baseline run directories are write-once.

## Target submission

Run this only after the configuration and prompts are frozen and the sealed
holdout has been evaluated once. It is the only entry point that opens
`dataset/messages.csv` for prediction:

```powershell
python -m notification_router.target --dataset-dir ../dataset --output ../dataset/output.csv --env-file ../.env --artifact-dir ../.artifacts/target-01 --cache-dir ../.artifacts/target-01/cache --run-id 20260802T-target-01 --max-cost-usd 1.00 --json
```

The target runner uses only the eleven label-free message fields, runs the
same deterministic joins, retrieval, packet, provider, safety, and finalization
stages, and writes no output until all 110 IDs and final fields validate. The
CSV is written through a temporary file and atomic replace, then reparsed with
the exact output schema and evidence allowlists. Raw provider attempts,
packets, final decisions, and operation accounting remain in the ignored,
write-once artifact directory. The target command never opens the sealed
holdout or reads evaluator labels.

## Milestone 2 evaluation boundary

`EvaluationHarness.router_inputs()` is the router-facing boundary. It exposes
only the eleven input columns; expected action, type, reason, confidence, and
evidence labels remain evaluator-side. Holdout rows require an explicit
evaluator reveal and are not returned by default.

The harness API is available from `notification_router.evaluation`, retrieval
from `notification_router.retrieval`, and packet assembly from
`notification_router.packet`. No model invocation is performed by these APIs.

## Deferred decisions

- Production OCR/ASR decoder behavior, cache encryption/retention, and live
  provider pricing remain operational decisions; the baseline preserves the
  declared media state when extraction is unavailable.
- Confidence remains the frozen `PROVISIONAL-V0` deterministic policy for this
  first baseline; no label-fitted calibration is performed.
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

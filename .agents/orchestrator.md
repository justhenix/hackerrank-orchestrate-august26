# Orchestrator Specification

> **Configuration status:** Unless marked `CONTRACT`, every numeric threshold, cap, top-K, evidence limit, retry limit, time window, and resource budget in this document is `PROVISIONAL-V0` and subject to baseline evaluation.

## Pipeline overview

```text
S0 manifest -> S1 load/validate -> S2 normalize/join -> S3 extract/cache
    -> S4 features/safety -> S5 retrieve/allowlist -> S6 packet
    -> S7 route -> S8 validate/adjust -> S9 CSV/evaluation artifacts
```

Stages run in this order. Parallel extraction MAY occur inside S3, but deterministic ordering MUST be restored before S4.

## Phased implementation milestones

Milestone numbers are sequencing labels, not numeric configuration.

### M0 - Contract harness

Deliver:

- CLI skeleton and run manifest;
- CSV schemas, typed loader, join/temporal validators;
- decision JSON schema and final CSV validator;
- synthetic fixtures for fatal contract errors.

Gate: deterministic contract tests pass. No model quality, dashboard, concurrency, or production cache backend is required.

### M1 - First runnable end-to-end baseline

Deliver the smallest complete Option C path:

- byte-signature media sniffing;
- one extractor adapter behind the `ExtractionRecord` interface;
- simple content-hash filesystem cache with versioned identity;
- deterministic context, non-embedding retrieval fallback, and evidence allowlist;
- one text router with raw-response preservation;
- provisional confidence policy and complete CSV validation;
- evaluator support for the `PROVISIONAL-V0 development_rows=20` partition only.

Gate: one terminal command produces a contract-valid development CSV and immutable raw artifacts. M1 MUST NOT wait for dashboards, distributed tracing, encrypted/shared cache, embedding retrieval, parallelism, broad language coverage, or comprehensive security hardening.

### M2 - Development baseline quality

Deliver:

- all required media states and mixed-extension regressions;
- generalized safety positive/negative controls;
- confidence monotonicity tests;
- development metrics, cost, latency, and stage error summaries;
- reproducibility from a warm extraction cache.

Gate: all development and regression acceptance checks pass without message-ID or label-specific patches.

### M3 - Optional quality improvements

Candidate work, evaluated independently:

- embedding retrieval;
- stronger OCR/ASR/VLM extraction adapters;
- provider/model comparison;
- class-level or global calibration;
- bounded concurrency and richer cache backends.

Each change requires versioned configuration and a fresh unpatched development run. None is required for the first runnable baseline.

### M4 - Hardening and observability

Deliver as justified by risk/budget:

- encrypted cache and retention controls;
- structured tracing, dashboards, and cost alarms;
- prompt-injection fuzzing and dependency hardening;
- outage, corruption, rate-limit, and atomic-write tests;
- packaging verification using only `code/` runtime assets.

Core immutable run records and error codes are required earlier; optional dashboards and production-grade telemetry are not.

### M5 - Configuration freeze and sealed evaluation

Freeze every item required by `evaluation-rubric.md`, run all `DATASET FACT sample_rows=30` once, reveal the sealed holdout only through the evaluator, and publish development/holdout/combined metrics separately. Holdout results are reporting-only and cannot change this submission.

## S0 - Run manifest and isolation

**Inputs**

- explicit dataset directory;
- explicit input filename (`messages.csv` or the evaluator's sanitized sample input);
- runtime configuration;
- pinned schema, prompt, extractor, router, and confidence-policy versions.

**Outputs**

- immutable run ID;
- canonical configuration and SHA-256;
- allowlisted input paths;
- input-file SHA-256 inventory;
- environment capability report.

**Responsibilities**

- Refuse recursive discovery outside allowlisted participant files.
- Record versions without recording secrets.
- Verify required runtime prompts/schemas/configuration are available from the submitted `code/` package.

**Failure states**

- `CFG_INVALID`, `INPUT_MISSING`, `CAPABILITY_MISSING`, `SECRET_MISSING`.

**Fallback**

- None for invalid configuration or missing required input; fail the run before predictions.
- Optional extraction capabilities may continue only if S3 can emit the exact `unsupported` state.

**Tests**

- Reject unknown input paths and organizer-only paths.
- Configuration hash is stable under key-order changes.
- Secret values never appear in the manifest.
- Same files/configuration produce the same run identity inputs.

## S1 - CSV loading and schema validation

**Inputs**

- allowlisted CSV paths;
- exact schema definitions.

**Outputs**

- typed immutable tables;
- validation report with row counts, key counts, missingness, and domain checks.

**Responsibilities**

- Parse UTF-8 CSV deterministically.
- Enforce exact required columns and types.
- Enforce primary and composite uniqueness.
- Validate enum domains and conditional conversation/media fields.
- Separate sample labels in the evaluator before router-visible input exists.

**Failure states**

- `CSV_PARSE_FAILED`, `SCHEMA_MISSING_COLUMN`, `SCHEMA_EXTRA_COLUMN`, `TYPE_INVALID`, `ENUM_INVALID`, `DUPLICATE_KEY`, `CONDITIONAL_FIELD_INVALID`.

**Fallback**

- None for target, history, or identity-table contract failures; fail the run.
- Optional context fields may remain null only where the source contract permits null.

**Tests**

- Every declared schema has valid/invalid fixtures.
- Duplicate IDs and duplicate composite relationships fail.
- Voice rows with text absent remain valid; mismatched media type/ID fails.
- Sample output columns are unavailable to all downstream router functions.

## S2 - Normalization, joins, and temporal views

**Inputs**

- typed tables from S1;
- incoming message timestamp.

**Outputs**

- one immutable `MessageContext` per incoming message;
- join-coverage report;
- prior-only history and daily-summary views.

**Responsibilities**

- Normalize timestamps without changing source values.
- Join user, group, group-member, business, and user-business context with explicit keys.
- Enforce same-user and strictly-prior temporal views.
- Represent absent optional context as `unknown`, never as a negative fact.

**Failure states**

- `REFERENCE_BROKEN`, `TIMESTAMP_INVALID`, `REQUIRED_JOIN_MISSING`, `OPTIONAL_CONTEXT_MISSING`.

**Fallback**

- Broken required identities fail the run.
- Optional relationship absence continues with a missing-context flag and confidence penalty.
- Offset-free timestamps remain dataset-local naive wall-clock values. Code does not infer a geographic timezone, attach an offset, or convert source values; quiet hours and all comparisons use the same convention.

**Tests**

- Cross-user and same-time/future history never enters the view.
- Missing optional business relationship does not imply opt-out, distrust, or spam.
- Quiet-hour windows crossing midnight are parsed correctly.
- Target rows are never appended to history during a run.

## S3 - Media sniffing, extraction, and cache

**Inputs**

- message media reference;
- media metadata;
- raw media bytes;
- extractor configuration and versions.

**Outputs**

- exactly one `ExtractionRecord` from `decision-contract.md` per media item;
- `media_state=not_applicable` with no extraction record for text-only messages;
- immutable extraction trace;
- cache hit/miss status.

**Responsibilities**

- Detect format from byte signature, not extension.
- Check decoder capability before extraction.
- Isolate decoding from semantic extraction.
- Use content/version/config identity for cache lookup.
- Assign exactly one media state.

**Failure states**

- `MEDIA_MISSING`, `MEDIA_UNSUPPORTED`, `MEDIA_DECODE_FAILED`, `MEDIA_EMPTY_EXTRACTION`, `MEDIA_LOW_QUALITY`, `EXTRACTOR_TIMEOUT`, `EXTRACTOR_RATE_LIMITED`, `EXTRACTOR_SCHEMA_INVALID`, `CACHE_CORRUPT`.

**Fallback**

- `missing`, `unsupported`, and `decode_failed`: continue with empty semantic fields and explicit state.
- `empty_extraction`: continue with the explicit state and no invented transcript/description.
- `low_quality`: continue with extracted content, quality reasons, and confidence penalty.
- Retry transient extractor failures within configured limits; after exhaustion map them to `decode_failed` only when decoding failed, otherwise `empty_extraction`. Preserve the original error code.
- Text accompanying media remains available; unusable media never erases valid message text.

**Tests**

- PNG/WebP/AVIF bytes named `.jpg` use correct decoders.
- WAV/M4A bytes named `.mp3` use correct decoders.
- Cache hits require identical content and every version/config component.
- Corrupt cache entries are quarantined and recomputed, never trusted.
- Each required media state has a fixture.
- Extraction prompt injection remains quoted data.

## S4 - Deterministic features and safety constraints

**Inputs**

- `MessageContext`;
- `ExtractionRecord`;
- configured invariant definitions.

**Outputs**

- typed deterministic features;
- context completeness score inputs;
- `SafetyConstraints` with allowed/prohibited/required actions and trigger provenance.

**Responsibilities**

- Compute quiet-hour, forwarding, relationship, business identity/domain, notification-load, engagement, and media-quality signals.
- Detect exact `@user_id` mentions deterministically in raw `message_text` using `safety-invariants.md`; record offsets. Extracted-media mention candidates remain non-bypass semantic signals.
- Apply `safety-invariants.md` positive triggers and negative controls.
- Never treat a single loose keyword as a routing override.

**Failure states**

- `FEATURE_COMPUTE_FAILED`, `INVARIANT_CONFIG_INVALID`, `INVARIANT_CONTRADICTION`.

**Fallback**

- Required feature/invariant configuration failures fail the run.
- Optional feature failures become explicit missing context; they cannot silently default to favorable or adverse values.
- Contradictory hard invariants prohibit `notify` and require bounded review/error handling; they are never resolved by rule order.

**Tests**

- Every deterministic rule has positive and negative-control fixtures.
- Missing domain/relationship stays unknown.
- One risk token alone cannot require mute.
- Model-derived indirect addressing alone cannot bypass quiet hours or muted-group constraints.
- Trusted transactional updates are not treated as promotions solely because they mention money.

## S5 - Retrieval and evidence allowlisting

**Inputs**

- incoming context and extracted semantics;
- strictly-prior same-user history view;
- history events;
- retrieval configuration.

**Outputs**

- deterministically ordered `HistoricalCandidate` list;
- ordered `allowed_evidence_message_ids`.

**Responsibilities**

- Generate candidates by relationship scope before semantic ranking.
- Calculate versioned relationship, recency, semantic, and behavioral components.
- Enforce same-user, prior-only, existing-ID constraints.
- Apply stable tie-breaking.

**Failure states**

- `RETRIEVER_FAILED`, `EMBEDDING_FAILED`, `NO_CANDIDATES`, `CANDIDATE_INVALID`.

**Fallback**

- If embeddings fail, use the documented deterministic non-embedding score from relationship, recency, and behavior.
- `NO_CANDIDATES` is valid and produces an empty allowlist, not an error row.
- Invalid candidates are excluded and recorded; systematic invalidity fails the stage.

**Tests**

- Cross-user, future, same-time, unknown, and target IDs are excluded.
- Equal scores have stable ordering.
- Retrieval does not access sample labels.
- An empty allowlist remains valid.

## S6 - Routing packet assembly

**Inputs**

- message/context;
- extraction record;
- deterministic features and constraints;
- historical candidates and allowlist;
- prompt and schema versions.

**Outputs**

- canonical `RoutingPacket`;
- packet SHA-256;
- routing cache identity.

**Responsibilities**

- Enforce token/character budgets by deterministic field truncation rules that preserve IDs and constraint fields.
- Separate instructions from all untrusted content.
- Include only fields permitted by the contract.

**Failure states**

- `PACKET_SCHEMA_INVALID`, `PACKET_TOO_LARGE`, `PACKET_SERIALIZE_FAILED`.

**Fallback**

- Deterministically shorten candidate content summaries and drop lowest-ranked candidates until within budget.
- Never drop safety constraints, media state, incoming text, or allowlist consistency.
- If the minimal packet remains too large, fail the message into declared degraded routing.

**Tests**

- Canonical serialization is byte-stable.
- Prompt-like message/media text cannot escape the data envelope.
- Truncation preserves candidate/allowlist equality and ranking.

## S7 - One text routing model

**Inputs**

- canonical routing packet;
- pinned router, prompt, and JSON schema.

**Outputs**

- immutable raw response bytes;
- parsed `RawRoutingDecision` or error;
- latency, token usage, retry count, and routing cache status.

**Responsibilities**

- Perform semantic interpretation and propose the single final structured routing decision.
- Select evidence only from the supplied allowlist.
- Report routing uncertainty and semantic flags.

**Failure states**

- `ROUTER_TIMEOUT`, `ROUTER_RATE_LIMITED`, `ROUTER_UNAVAILABLE`, `ROUTER_SCHEMA_INVALID`, `ROUTER_REFUSAL`, `ROUTER_CACHE_CORRUPT`.

**Fallback**

- Retry only transient failures using configured deterministic backoff and attempt limits.
- A schema-invalid response receives at most `PROVISIONAL-V0 schema_repair_limit=1` repair attempt using the same packet/schema and no label feedback.
- Retry exhaustion invokes the declared degraded routing contract; every attempt remains immutable.

**Tests**

- Fabricated or out-of-allowlist evidence is rejected.
- Invalid enums, confidence fields, excessive reasons, and malformed JSON fail schema.
- Identical packet/model/prompt/config identities produce cache-compatible results.
- Router never receives expected labels.

## S8 - Safety validation and confidence finalization

**Inputs**

- raw routing decision;
- routing packet;
- extraction quality;
- evidence candidates;
- confidence policy.

**Outputs**

- valid `FinalDecision` or validation error;
- confidence component audit;
- triggered invariant audit.

**Responsibilities**

- Validate action against allowed/prohibited/required constraints.
- Validate evidence subset, provenance, order, and relevance claim availability.
- Apply semantic-flag invariants with structured positive triggers and negative controls.
- Recompute contradiction count deterministically; never trust the model-reported count for confidence.
- Compute final confidence deterministically.
- Preserve raw response before any retry or fallback.

**Failure states**

- `ACTION_CONSTRAINT_VIOLATION`, `EVIDENCE_NOT_ALLOWED`, `REASON_INVALID`, `SEMANTIC_FLAG_CONTRADICTION`, `CONFIDENCE_INPUT_INVALID`.

**Fallback**

- Do not silently rewrite a valid-looking but invariant-violating model decision.
- Return the validation error to S7 for `PROVISIONAL-V0 constraint_retry_limit=1`; then use declared degraded routing.
- Confidence computation errors use `PROVISIONAL-V0 confidence=0.05` only for an otherwise valid decision and emit a critical audit error.

**Tests**

- Raw model uncertainty is never copied directly to final confidence.
- Low extraction quality, weak evidence, missing context, contradictions, and uncertainty lower confidence monotonically.
- A model-selected unknown/future/cross-user ID cannot reach CSV output.

## S9 - CSV and evaluation artifacts

**Inputs**

- one final decision per target;
- immutable run manifest and raw artifacts.

**Outputs**

- exact-contract `output.csv`;
- validation report;
- immutable raw baseline bundle and metrics when in evaluation mode.

**Responsibilities**

- Enforce exact columns/order, target ID equality, uniqueness, enums, confidence range, reason bounds, and evidence serialization.
- Write atomically only after complete validation.
- Keep raw and adjusted evaluation layers separate.

**Failure states**

- `OUTPUT_ROW_COUNT_INVALID`, `OUTPUT_ID_SET_INVALID`, `OUTPUT_DUPLICATE_ID`, `OUTPUT_COLUMN_INVALID`, `OUTPUT_VALUE_INVALID`, `OUTPUT_WRITE_FAILED`, `BASELINE_MUTATION_ATTEMPT`.

**Fallback**

- No partial submission CSV.
- Any validation failure blocks publication and leaves the previous valid output untouched.
- Baseline mutation attempts fail evaluation immediately.

**Tests**

- Exact 1:1 ID coverage and order-independent set equality.
- `none` evidence serialization and semicolon validation.
- Atomic write interruption cannot leave a valid-looking partial file.
- Re-running metric code cannot mutate raw responses or baseline predictions.

# Message Notification Router

This is the interview guide for the submitted Candidate V1 system. The verified
`code.zip` SHA-256 is `dea73181e75b1848d792700f91797cae18541342755123dfea2f8f85eb8535fc`.
The source package is the authority for behavior. This guide separates what is
implemented from what is planned, limited, or a reasonable next step.

Source references use paths inside the submitted archive, such as
`notification_router/retrieval.py::retrieve_history`.

## 1. Two-Minute Pitch

WhatsApp puts family messages, group chatter, business promotions, posters,
voice notes, and possible scams in the same stream. If every message produces
the same interruption, people miss important information or become numb to
notifications. The router turns that overload into three choices: `notify` when
the user should be interrupted now, `digest` when the message can wait, and
`mute` when it is low-value, repetitive, suspicious, or unsafe.

The submitted system accepts text, images, and voice notes. It combines the
receiving user, sender or group relationship, business context, prior messages,
interaction events, and media interpretation. Gemini is used for the part that
requires semantic understanding: extracting meaning from media and proposing a
structured action and message type. Deterministic Python then controls the
parts that must be auditable: CSV validation, relationship joins, timestamp
boundaries, evidence provenance, safety constraints, confidence calculation,
fallback behavior, and the final CSV contract.

What was actually submitted is a runnable Python backend package, offline tests,
Vertex Gemini integration, immutable run artifacts, and a validated 110-row
`output.csv`. On the 20-row development baseline, action accuracy was 0.8000
and action macro-F1 was 0.7985. The sealed holdout was weaker at 0.4000 action
accuracy, so this is evidence of a disciplined prototype, not proof of perfect
personalization. Six target rows used explicit degraded fallbacks, although all
110 rows still passed the output contract. The main product limitation is that
there is no live WhatsApp Web adapter, browser extension, or user interface.
The future direction is an adapter that maps live WhatsApp Web events into the
same label-free packet and sends the validated action back to the interface.
That adapter is not part of the submission.

## 2. What Was Built

The submitted scope is a backend pipeline. Each component has a narrow contract
and records enough information to explain failure without changing the original
model attempt.

### 2.1 Deterministic CSV and relationship validation

**Purpose.** Stop malformed or inconsistent data before it reaches routing.

**Input.** The registered participant CSV files under `dataset/`, including
messages, users, groups, group membership, business accounts, business history,
historical messages, events, media catalogs, and notification summaries.

**Output.** Typed immutable rows, or a `DatasetValidationError` containing
bounded validation issues.

**Source.** `notification_router/schemas.py::load_csv_table`,
`notification_router/dataset.py::load_dataset`,
`notification_router/dataset.py::validate_key_integrity`, and
`notification_router/dataset.py::validate_referential_integrity`.

**What can fail and handling.** Missing files, wrong UTF-8 or CSV structure,
wrong column order, invalid types or enums, duplicate primary or composite keys,
broken foreign keys, invalid conditional relationships, and media paths that
escape the dataset root fail closed. No routing packet is built after these
errors. The tests
`tests/test_milestone1.py::MilestoneOneTests.test_exact_schema_rejects_missing_extra_and_reordered_columns`,
`test_schema_types_enums_and_composite_keys_are_checked`, and
`test_referential_integrity_rejects_unknown_user_and_unsafe_media_path` cover
these checks.

### 2.2 Normalization and nullable joins

**Purpose.** Turn separate tables into explicit relationships that routing code
can inspect without repeatedly joining raw CSV rows.

**Input.** Typed `DatasetTables` from the validated loader.

**Output.** A `NormalizedDataset` containing message contexts, per-user history
indexes, event lookup, and join coverage. Optional relationships remain explicit
as `None` rather than being silently invented.

**Source.** `notification_router/dataset.py::normalize_dataset` and the
immutable data classes in `notification_router/models.py`, especially
`MessageContext`, `JoinCoverage`, and `NormalizedDataset`.

**What can fail and handling.** A failed key or relationship check raises before
normalization. A valid but absent optional relationship is recorded in
`JoinCoverage.optional_missing` and remains absent in the packet. The real-data
test `tests/test_milestone1.py::MilestoneOneTests.test_real_dataset_loads_with_typed_rows_and_joins`
checks typed rows and join coverage.

### 2.3 Timestamp policy

**Purpose.** Make ordering reproducible without guessing a geographic timezone.

**Input.** CSV timestamps in `YYYY-MM-DD HH:MM` form and model timestamps that
must correspond to the same source convention.

**Output.** Naive Python `datetime` values under the policy
`dataset-local-naive-wall-clock`.

**Source.** `notification_router/schemas.py::FieldSpec.parse`,
`notification_router/dataset.py::DATASET_WALL_CLOCK_POLICY`, and the
timestamp checks in `notification_router/contracts.py::parse_extraction_record`
and `parse_routing_decision`.

**What can fail and handling.** Bad formats or timezone offsets are rejected.
The system does not attach an offset or convert a source timestamp. The test
`tests/test_milestone3a.py::MilestoneThreeATests.test_dataset_local_timestamps_reject_timezone_offsets`
checks both extraction and routing timestamps.

### 2.4 Byte-signature media detection

**Purpose.** Identify whether an image or voice file is really a supported media
format before sending bytes to an extractor.

**Input.** A media catalog path and the first 64 bytes of the file.

**Output.** A `MediaSniffResult` containing detected format, extension format,
signature state, type match, safe resolved path, and an error code when needed.

**Source.** `notification_router/media.py::sniff_bytes` and
`notification_router/media.py::sniff_media_file`.

**What can fail and handling.** Missing files, unsafe paths, read errors,
unknown signatures, or a media-type mismatch produce a visible media state.
The extension is reported for diagnostics but is not trusted as the format.
The test `tests/test_milestone1.py::MilestoneOneTests.test_media_sniffing_uses_bytes_not_extensions`
uses a deliberately disguised file, and
`test_media_catalog_is_recognized_and_reports_extension_mismatches` checks the
catalog aggregate.

### 2.5 Multimodal extraction

**Purpose.** Convert image or voice bytes into bounded text and factual
description fields that the routing stage can read.

**Input.** A `notification_router/providers.py::ExtractionRequest` containing media ID, declared type and path,
byte-detected format, content hash, bytes, and the incoming timestamp.

**Output.** A contract-checked `ExtractionRecord` with extraction state,
extracted text, factual description, language, quality score, and provenance
fields.

**Source.** `notification_router/providers.py::MultimodalExtractionProvider`,
`notification_router/providers.py::ExtractionRequest`,
`notification_router/providers.py::FakeMultimodalProvider`,
`notification_router/integration.py::ModelIntegrationClient.extract`, and
`notification_router/gemini.py::GoogleGeminiProvider.extract`.

**What can fail and handling.** Unsupported or missing media gets a deterministic
record with a visible state such as `missing` or `unsupported`. A provider
timeout, rate limit, unavailable service, invalid JSON, or request mismatch is
retried within the configured bound. A final provider failure becomes an
`empty_extraction` record with low quality, so the route stage can still use
message text when available. The extraction parser rejects output that changes
the requested media ID, path, format, content hash, or timestamp.

### 2.6 Historical retrieval

**Purpose.** Supply a small, reproducible set of prior interactions that can
support personalization and evidence.

**Input.** One incoming message, normalized history, and `notification_router/retrieval.py::RetrievalConfig`.

**Output.** Up to 12 `HistoricalCandidate` records plus an ordered evidence
allowlist.

**Source.** `notification_router/retrieval.py::retrieve_history`,
`notification_router/retrieval.py::RetrievalConfig`, and
`notification_router/retrieval.py::validate_evidence_allowlist`.

**What can fail and handling.** A candidate with the wrong user, a timestamp
that is not strictly earlier, duplicate IDs, or an unstable ranking raises
`EvidenceProvenanceError`. The implementation uses a deterministic score with
relationship, recency, and behavioral components. Semantic similarity is
explicitly zero in this version. The tests in
`tests/test_milestone2.py::MilestoneTwoTests.test_retrieval_is_same_user_prior_only_and_non_embedding`,
`test_retrieval_excludes_same_time_future_and_cross_user_rows`, and
`test_equal_score_ordering_uses_timestamp_then_message_id` cover the boundary.

### 2.7 Routing packet construction

**Purpose.** Present the model with one canonical, label-free description of
the current message and its safe context.

**Input.** Sanitized message fields, normalized context, media state, extraction
record, deterministic features, safety constraints, and retrieval result.

**Output.** An immutable `RoutingPacket` with a canonical JSON representation,
SHA-256 hash, and a separate instruction/data envelope.

**Source.** `notification_router/packet.py::assemble_routing_packet`,
`notification_router/packet.py::RoutingPacket.prompt_envelope`,
`notification_router/packet.py::RoutingPacket.sha256`, and
`notification_router/packet.py::validate_routing_packet`.

**What can fail and handling.** A packet with unexpected top-level keys,
forbidden output-label keys, inconsistent message or media identity, fabricated
evidence, or unstable serialization raises `PacketValidationError`. Message,
history, and metadata text are nested as data, not interpolated into the static
instructions. `tests/test_milestone2.py::MilestoneTwoTests.test_routing_packet_is_canonical_label_free_and_prompt_contained`
and `test_packet_rejects_fabricated_allowlist_entry` cover this boundary.

### 2.8 Gemini structured routing

**Purpose.** Interpret the packet's content and propose one action, one message
type, a short reason, evidence selections, uncertainty, semantic flags, and
support spans.

**Input.** The canonical routing packet and the routing JSON Schema.

**Output.** A `RawRoutingDecision`. It intentionally has no final confidence
field.

**Source.** `notification_router/gemini.py::GoogleGeminiProvider.route`,
`notification_router/contracts.py::routing_response_schema`, and
`notification_router/contracts.py::parse_routing_decision`.

**What can fail and handling.** Missing configuration, provider errors, invalid
JSON, extra or missing fields, invalid enum values, overlong reasons, invalid
evidence, contradictory semantic support, and out-of-bounds spans are rejected.
`notification_router/integration.py::ModelIntegrationClient._invoke` preserves the raw response, sends bounded
machine-readable validation feedback on a retry, and then returns an explicit
integration error if the contract still fails. Gemini is selected explicitly as
Vertex AI or AI Studio in `notification_router/gemini.py::build_gemini_provider_bundle`.

### 2.9 Evidence allowlisting

**Purpose.** Ensure model-selected history IDs can only come from the safe
candidate set prepared for the current receiving user.

**Input.** The retrieval candidates, their ordered allowlist, and the raw model
selection.

**Output.** Zero to three selected IDs in the same order as the allowlist, or a
visible validation error.

**Source.** `notification_router/retrieval.py::validate_selected_evidence`,
`notification_router/contracts.py::parse_routing_decision`, and
`notification_router/finalization.py::validate_routing_safety`.

**What can fail and handling.** Unknown, duplicate, reordered, too many, or
cross-boundary IDs raise `EVIDENCE_NOT_ALLOWED` or
`EvidenceProvenanceError`. The route is retried when possible. A final route
failure uses the declared degraded fallback with no selected evidence. The
test `tests/test_milestone2.py::MilestoneTwoTests.test_evidence_allowlist_and_selected_evidence_fail_closed`
checks fabricated, duplicated, and reordered selections.

### 2.10 Safety and semantic consistency validation

**Purpose.** Recompute high-impact constraints in Python rather than trusting a
free-form model proposal.

**Input.** The current message, packet constraints, raw decision, deterministic
features, and retrieval result.

**Output.** A `SafetyAudit` or a bounded `StructuredOutputError`.

**Source.** `notification_router/features.py::compute_deterministic_features`
and `notification_router/finalization.py::validate_routing_safety`.

**What can fail and handling.** The validator rejects a prohibited `notify`, a
corroborated high-risk request that is not muted, a non-transactional promotion
after a prior opt-out that is not muted, invalid evidence, instruction-like
reasons, evidence IDs copied into the reason, or three or more independently
recomputed semantic contradictions. The provider integration retries a
structured violation, then `notification_router/finalization.py::degraded_final_decision` creates a visible fallback.

### 2.11 Deterministic confidence finalization

**Purpose.** Produce a consistent confidence value that reflects uncertainty,
media quality, evidence strength, context completeness, contradictions, and
retry history.

**Input.** The accepted raw decision, packet, deterministic features, extraction
state, retrieval packet, and attempt count.

**Output.** A final confidence and a detailed `ConfidenceAudit`.

**Source.** `notification_router/confidence.py::calculate_final_confidence`
and `notification_router/finalization.py::finalize_routing_decision`.

**What can fail and handling.** The formula is deterministic and bounded between
0.05 and 0.98, with additional media and degraded caps. It is not a trained
calibrator and has no hidden label-fitting step. The model's `routing_uncertainty`
is an input, but a model-supplied confidence field is not accepted. The test
`tests/test_milestone4a.py::MilestoneFourATests.test_confidence_is_monotonic_and_audited`
checks monotonic behavior and audit output.

### 2.12 Degraded fallback behavior

**Purpose.** Keep the output contract complete when semantic routing is
unavailable, without pretending that a fallback is a successful prediction.

**Input.** A validated packet, available features and media state, retrieval
result, and an error code.

**Output.** `action=digest`, `message_type=unknown`, the reason
`Routing unavailable; queued safely for later review.`, confidence capped at
0.10, no selected evidence, and `degraded=true` in artifacts.

**Source.** `notification_router/finalization.py::degraded_final_decision`
and `notification_router/baseline.py::run_partition_baseline`.

**What can fail and handling.** The fallback itself is deterministic. A required
stage failure or systematic contract failure can abort a run; the target runner
then refuses to write a partial output. A completed row with a route failure is
still emitted, but its degraded status and error code remain visible.

### 2.13 Extraction caching

**Purpose.** Reuse successful media extraction safely across runs without
assuming that a path or message ID identifies media content.

**Input.** Media content hash, detected format, provider and backend identity,
extractor version, configuration hash, schema version, and model name.

**Output.** A write-once cache entry containing the identity, validated record,
and raw extraction responses.

**Source.** `notification_router/extraction_cache.py::build_extraction_cache_identity`,
`notification_router/extraction_cache.py::ExtractionCache.lookup` or
`notification_router/extraction_cache.py::ExtractionCache.put`.

**What can fail and handling.** Missing content is not cached. An identity
mismatch or malformed entry is quarantined and the provider path is used again.
An existing valid entry is never silently overwritten. The test
`tests/test_milestone4a.py::MilestoneFourATests.test_extraction_cache_uses_content_and_configuration_identity`
checks reuse for different paths and invalidation for changed content.

### 2.14 Immutable artifacts and diagnostics

**Purpose.** Preserve the evidence needed to debug a run and reproduce what the
system saw, while preventing accidental overwrites.

**Input.** Run configuration, packet hashes, raw provider attempts, row records,
final decisions, accounting, errors, and metrics.

**Output.** A write-once manifest, raw prediction JSONL, per-row artifacts,
error JSONL, metrics, and submission metadata.

**Source.** `notification_router/artifacts.py::ImmutableArtifactStore`,
`notification_router/artifacts.py::build_label_free_run_manifest`, and the artifact-writing paths in
`notification_router/baseline.py::run_partition_baseline`.

**What can fail and handling.** Attempts to overwrite an artifact or escape the
run root raise `ImmutableArtifactError`. Raw provider bytes are passed to the
sink before parsing, so an invalid response is still diagnosable. The tests
`tests/test_milestone2.py::MilestoneTwoTests.test_manifests_and_raw_predictions_are_write_once`
and `tests/test_milestone3c.py::MilestoneThreeCTests.test_raw_response_sink_writes_before_invalid_output_validation`
cover these properties.

### 2.15 Atomic final `output.csv` validation

**Purpose.** Ensure the submitted file has exactly one valid row per target
message and cannot be left half-written.

**Input.** Final `RawPrediction` objects, expected target IDs, and per-message
evidence allowlists.

**Output.** An atomically replaced CSV and a re-parsed `SubmissionArtifact`.

**Source.** `notification_router/submission.py::validate_predictions`,
`notification_router/submission.py::write_output_csv`, and
`notification_router/submission.py::validate_output_csv`, called from
`notification_router/target.py::run_target_submission`.

**What can fail and handling.** Wrong row count or ID set, duplicate IDs,
invalid enums, invalid confidence, invalid reason, or invalid evidence prevents
the write. The writer uses a temporary file, flushes and syncs it, replaces the
destination, then reparses and validates the exact output schema. If the
post-write hash or row count changes, the run fails. The tests in
`tests/test_submission.py::SubmissionTests.test_exact_output_is_atomic_and_reparseable`
and the two rejection tests cover this contract.

### 2.16 Offline tests

The package contains 55 offline `unittest` cases across seven test files. The
verified project report records all 55 as passing. They cover data contracts,
temporal boundaries, retrieval provenance, packet containment, mocked Gemini
adapters, retries, caching, artifacts, confidence, and final CSV validation.
The live provider adapter tests use mocked SDK objects and do not make network
calls. The submitted package is not described as production-ready because the
test suite is offline and the target run still had degraded rows.

## 3. End-to-End Data Flow

The path for one incoming message is:

```text
CSV row and media catalog
  -> exact schema and relationship validation
  -> typed rows and nullable joins
  -> dataset-local timestamp policy
  -> byte-signature media sniffing
  -> same-user, strictly-prior retrieval
  -> deterministic features and routing packet
  -> Gemini extraction when media exists
  -> Gemini structured routing proposal
  -> deterministic parsing, provenance, and safety checks
  -> deterministic confidence or degraded fallback
  -> complete atomic output.csv validation
```

1. **Load and validate.** This is deterministic. `schemas.load_csv_table` reads
   exact headers, parses types and enums, and checks keys. `dataset.load_dataset`
   then checks foreign keys and conditional fields. The invariant is that no
   malformed table enters the pipeline. Source: `notification_router/schemas.py`
   and `notification_router/dataset.py`.

2. **Normalize context.** This is deterministic. `normalize_dataset` creates
   user, group, membership, business, business-history, event, and history
   indexes. The invariant is that optional relationships stay explicit and
   validated. Source: `notification_router/dataset.py::normalize_dataset`.

3. **Apply time policy.** This is deterministic. The incoming timestamp is kept
   as a dataset-local naive wall-clock value. The invariant is that no stage
   compares a timezone-converted value with a source value. Source:
   `notification_router/dataset.py::DATASET_WALL_CLOCK_POLICY` and
   `notification_router/contracts.py`.

4. **Sniff media.** This is deterministic. If media is declared, the system
   resolves the catalog path inside the dataset and reads up to 64 header bytes.
   The output is a `MediaSniffResult`. The invariant is that an extension alone
   cannot authorize extraction. Source: `notification_router/media.py`.

5. **Extract media.** This is model-driven when a supported image or voice file
   is sent to Gemini. The input is media bytes plus request-bound metadata. The
   output is an `ExtractionRecord`, or a visible media-state fallback. The
   invariant is that the response must copy the request identity and satisfy the
   extraction schema. Source: `notification_router/gemini.py` and
   `notification_router/integration.py::ModelIntegrationClient.extract`.

6. **Retrieve prior context.** This is deterministic. The input is the current
   message and normalized history. The output is a ranked candidate list and an
   allowlist of at most 12 IDs. The invariant is same receiving user and
   `historical.created_at < incoming.created_at`. Source:
   `notification_router/retrieval.py::retrieve_history`.

7. **Build the packet.** This is deterministic. It combines the current message,
   user and conversation context, media state, deterministic features, safety
   constraints, and candidates. The invariant is that the packet contains no
   expected action, type, reason, confidence, or selected-evidence label. Source:
   `notification_router/packet.py::validate_routing_packet`.

8. **Propose a route.** This is model-driven. Gemini receives the packet as data
   plus a strict JSON Schema and returns a `RawRoutingDecision`. The invariant is
   exact JSON keys, allowed enums, bounded reason, valid support spans, and
   allowlisted evidence. Source: `notification_router/contracts.py` and
   `notification_router/gemini.py::GoogleGeminiProvider.route`.

9. **Validate and finalize.** This is deterministic. Python recomputes safety
   conditions, validates semantic support against packet text, and calculates
   final confidence. The invariant is that unsafe or contradictory decisions do
   not silently pass. Source: `notification_router/finalization.py` and
   `notification_router/confidence.py`.

10. **Write the row.** This is deterministic. `notification_router/target.py` checks that every target
    row produced a final decision, validates the exact ID set and evidence
    allowlists, writes atomically, reparses, and checks the resulting hash. The
    invariant is that no partial or merely model-shaped CSV is submitted. Source:
    `notification_router/target.py::run_target_submission` and
    `notification_router/submission.py`.

## 4. Personalization Logic

The system personalizes by combining fields that are present in the submitted
data. It does not have a general-purpose preference profile or an onboarding
conversation.

### Implemented signals

- **Receiving user.** `users.csv` supplies the do-not-disturb window and recent
  opened, replied, dismissed, and reported counts. These are placed in the user
  context in `notification_router/packet.py`.
- **Sender relationship.** A personal sender ID is retained. Prior same-scope
  interaction can make `trusted_personal_sender` true when there was prior open
  or reply behavior and no prior report. Source:
  `notification_router/features.py::_prior_interactions` and `notification_router/features.py::compute_deterministic_features`.
- **Group context.** The group type, size, activity, membership role, read and
  reply behavior, and `group_muted_by_user` state are available. An admin sender
  is recognized as `trusted_group_sender`. A muted group normally prohibits
  `notify`, subject to narrow urgent or explicit-mention exceptions.
- **Business context.** Verification, official domain, sender domain, account
  age, user reports, and the receiver's business history are available. A
  verified, domain-matching account with activity history can be a trusted
  business. A domain mismatch plus a young sender domain or high reports can
  contribute to a high-risk safety path.
- **Prior interactions.** `message_history.csv` and `message_events.csv` are
  restricted to the receiving user and prior timestamps. Opens, replies,
  dismissals, muting, and reports affect retrieval behavior and deterministic
  trust or risk features.
- **Recency.** Retrieval gives newer eligible history a higher recency score in
  a 180-day window. It does not use future or same-time history.
- **Historical evidence.** The model can select up to three items from the
  deterministic top-12 allowlist. Evidence is provenance, not a free-form claim
  that the model can invent any ID.
- **Media content.** Gemini extraction contributes `extracted_text`,
  `factual_description`, language, state, and quality to the routing packet.
- **Explicit mention.** A deterministic regular expression detects an explicit
  `@user_id` mention in the current message text and records character spans. It
  can provide a narrow exception to a quiet-hours or muted-group `notify`
  prohibition.

The daily notification summary table is loaded and joined for coverage, but the
current routing packet and deterministic feature computation do not expose a
daily notification count as an action signal. That distinction matters in an
interview: the file is implemented in ingestion, but notification-load-aware
routing is not claimed as implemented.

### Three hypothetical examples

1. **Urgent direct message from a trusted context.** Suppose the receiver gets
   a direct message from a sender with earlier opened and replied messages. The
   current text semantically indicates a near deadline. Gemini can set
   `time_critical` and provide a supporting span. Python checks that the deadline
   is within two hours, applies the sender and receiver context, and permits
   `notify` if no high-risk or quiet-hours constraint blocks it. The model
   interprets urgency; Python decides whether the proposal is contract-safe.

2. **Useful but non-urgent promotion.** Suppose a verified business has a
   matching domain and the receiver has recent activity and has not opted out of
   promotions. Gemini can classify the content as `promotion` and propose
   `digest`. Retrieval may provide prior same-business evidence. If a valid prior
   opt-out exists and the message is promotional rather than transactional,
   `notification_router/features.py` and `notification_router/finalization.py` require `mute` instead. The business
   relationship is deterministic context; the content category is model
   interpretation.

3. **Suspicious or unsafe message.** Suppose an unfamiliar business uses a
   mismatched young domain and asks for a secret or payment through a suspicious
   link. Gemini must support the current-message semantic flags with spans. The
   deterministic high-risk rule then requires `mute` with a compatible type such
   as `scam`, `spam`, or `unknown`. If the model returns a contradictory or
   unsupported proposal, the validator rejects it and the bounded fallback is
   used instead of silently repairing it.

### Planned, not implemented

Explicit onboarding preferences such as liking one character or disliking
generic anime promotions are not represented in the submitted schemas, feature
calculation, or packet. The current system cannot honestly claim to know those
preferences. A product extension could add an explicit preference store, consent
and update controls, and a versioned preference summary in the packet. That is
future work, not Candidate V1 behavior.

## 5. Multimodal Processing

### Text

Text messages go directly into the `message.message_text` packet field. They do
not require an extraction call. Text is still treated as untrusted data in the
prompt envelope, so text cannot change the instructions by including instruction
language.

### Images and voice notes

The media catalogs provide IDs and relative paths. `notification_router/media.py::sniff_media_file`
resolves the path inside the dataset, reads only the first 64 bytes for format
sniffing, and recognizes the supported signatures:

- Images: JPEG, PNG, WebP, and AVIF.
- Audio: MP3, M4A, and WAV.

File extensions are not blindly trusted because a file can have a misleading
name. The code reports extension mismatch separately from byte-detected format.
This behavior is directly tested by
`tests/test_milestone1.py::MilestoneOneTests.test_media_sniffing_uses_bytes_not_extensions`.

The provider boundary is deliberately separate from routing:
`notification_router/providers.py::MultimodalExtractionProvider` defines extraction, while
`notification_router/providers.py::TextRoutingProvider` defines routing. The Gemini Vertex adapter
uses `google-genai` structured JSON for both operations. For extraction,
`notification_router/gemini.py::GoogleGeminiProvider.extract` sends media bytes as a typed image or
audio part and sends request metadata as a separate text part. For routing,
`GoogleGeminiProvider.route` sends the canonical packet as text.

The extraction cache key is a SHA-256 of an identity containing the content
hash, detected format, extractor name and version, extractor configuration hash,
extraction schema version, and model name. It deliberately does not use a media
path or message ID as the content identity. A cached record is rebound to the
current request's path, media ID, and timestamp without changing cached semantic
bytes. Source: `notification_router/extraction_cache.py` and
`notification_router/baseline.py::_rebind_cached_record`.

If extraction fails, the system preserves a visible media state such as
`missing`, `unsupported`, `decode_failed`, or `empty_extraction`. The routing
packet remains structurally valid, but confidence is reduced or capped. A final
routing failure is a separate degraded route decision. This prevents a media
failure from being confused with a successful semantic interpretation.

The implementation does not claim arbitrary file analysis. PDF, ZIP, DOCX,
XLSX, CSV, APK, and unknown file types are not in `notification_router/media.py::KNOWN_FORMATS` and
there is no submitted general document, archive, or application decoder. A
future extractor could add such formats only with new signatures, provider
contracts, security limits, and tests.

## 6. Retrieval and Evidence Provenance

Retrieval is deliberately simple and auditable. `retrieve_history` starts from
`NormalizedDataset.strictly_prior_history`, then applies the same-user filter
again. A candidate must satisfy:

```text
historical.user_id == incoming.user_id
and historical.created_at < incoming.created_at
```

The default top-K is 12. Each candidate receives a deterministic score:

```text
0.5 * relationship + 0.3 * recency + 0.2 * behavioral
```

The relationship component favors the same business, group, or sender, then
same-user same-conversation history, then general same-user history. Recency is
bounded to a 180-day window. Behavioral score uses event signals such as opened,
replied, dismissed, muted-after-message, or reported. `semantic` is explicitly
zero in this non-embedding version.

Ties are stable: higher score first, newer timestamp next, then ascending
message ID. The allowlist is exactly the candidate order. The model can select
no more than three IDs, cannot duplicate them, and cannot reorder them. The
parser and the final safety validator both enforce these conditions.

### Why the model cannot select arbitrary IDs

The packet contains `allowed_evidence_message_ids`, and the response parser is
called with that allowlist. `parse_routing_decision` rejects an ID outside it.
`validate_selected_evidence` also checks uniqueness, maximum count, and order.
`validate_evidence_allowlist` checks that the allowlist itself came from actual
history, the correct user, and strictly earlier timestamps. This is a defense in
depth boundary, not a prompt-only request.

### Hypothetical evidence example

Imagine a receiver gets a new message from business `B` at 15:00. Retrieval may
rank an earlier message from the same receiver and business at 13:00 because it
has a strong relationship and recent interaction. That candidate can appear in
the allowlist and be selected as evidence. A message from another receiver, a
message at 16:00, or a same-time message cannot enter the allowlist, even if the
model could guess its ID.

### Why deterministic non-embedding retrieval was selected

The chosen approach needs no vector database, embedding model, embedding
version, or extra semantic distance to reproduce. It makes leakage boundaries
easy to test and gives every evidence ID a clear source. The cost is semantic
recall: a relevant older message with different wording may not rank near the
top. The evidence metrics show this tradeoff. Development evidence allowlist
validity was 1.0000, but exact evidence-set precision was 0.1930 and recall was
0.5238. Provenance validity and semantic usefulness are different metrics.

## 7. Safety and Failure Handling

The system follows a fail-closed principle for contracts and safety. It rejects
unsafe or unverifiable model output rather than inventing a correction. For an
unavailable route, it uses an explicit low-confidence digest fallback so a
potentially important message is not silently discarded. That fallback is safe
in the sense of being visible and auditable, not safe in the sense of being
semantically correct.

| Failure or risk | Detection point | Error code if available | Fallback behavior | Reason for the design |
|---|---|---|---|---|
| Invalid dataset schema | `notification_router/schemas.py::load_csv_table` and `notification_router/dataset.py::load_dataset` | `SCHEMA_MISSING_COLUMN`, `SCHEMA_EXTRA_COLUMN`, `SCHEMA_COLUMN_ORDER_INVALID`, `TYPE_INVALID`, or `ENUM_INVALID` | Stop before routing; no output | Bad structure should not become model context. |
| Missing relationship | `notification_router/dataset.py::validate_referential_integrity` | `REFERENCE_BROKEN` or `CONDITIONAL_FIELD_INVALID` | Stop before normalized joins | A guessed relationship can contaminate personalization and evidence. |
| Unsupported or malformed media | `notification_router/media.py::sniff_media_file` and `notification_router/baseline.py` S3 handling | `MEDIA_MISSING`, `MEDIA_READ_FAILED`, `MEDIA_TYPE_FORMAT_MISMATCH`, or `MEDIA_UNSUPPORTED` | Emit visible media state; continue with reduced media quality when possible | Preserve the row while showing that media meaning is unavailable. |
| Provider timeout | Provider adapter and `notification_router/integration.py::ModelIntegrationClient._invoke` | `PROVIDER_TIMEOUT`, normalized to `EXTRACTOR_TIMEOUT` or `ROUTER_TIMEOUT` | Retry when retryable; then media degradation or degraded digest fallback | Bound waiting and avoid silent loss. |
| Rate limiting | `notification_router/gemini.py::_provider_call_error` status normalization | `PROVIDER_RATE_LIMITED`, normalized to `EXTRACTOR_RATE_LIMITED` or `ROUTER_RATE_LIMITED` | Bounded retry; then explicit fallback | Protects spend and provider stability. |
| Invalid model JSON | Strict JSON parser in `notification_router/contracts.py` | `JSON_INVALID` or `SCHEMA_INVALID`, normalized by stage | Preserve raw response; retry with machine feedback; then fallback | Malformed output must remain visible, not be patched. |
| Invalid evidence ID | `notification_router/contracts.py::parse_routing_decision` and `notification_router/finalization.py::validate_routing_safety` | `EVIDENCE_NOT_ALLOWED` | Retry if possible; otherwise no selected evidence in fallback | Prevents arbitrary, cross-user, or future evidence. |
| Semantic flag contradiction | `notification_router/finalization.py::validate_routing_safety` | `SEMANTIC_FLAG_CONTRADICTION` | Retry if possible; otherwise degraded fallback | Contradictory hard signals are not trustworthy enough to route normally. |
| Action constraint violation | `notification_router/finalization.py::validate_routing_safety` | `ACTION_CONSTRAINT_VIOLATION` | Retry if possible; otherwise degraded fallback | Quiet hours, muted groups, high risk, and opt-out rules are deterministic boundaries. |
| Incomplete target run | `notification_router/baseline.py::run_partition_baseline` and `notification_router/target.py::run_target_submission` | `BaselineAbortedError` or a required-stage error | Refuse to write `output.csv` | A partial file cannot satisfy the one-row-per-message contract. |
| Invalid final CSV | `notification_router/submission.py::validate_predictions` and `notification_router/submission.py::validate_output_csv` | `SubmissionValidationError` | Refuse or remove temporary file; do not accept the artifact | Output shape is necessary for evaluation, even when semantics are imperfect. |

There is one important safety tradeoff to explain clearly. A valid high-risk
proposal with corroborating deterministic signals must be `mute`. If Gemini is
unavailable, the generic degraded fallback is `digest, unknown`, not an
automatic mute, because the system does not have enough semantic evidence to
classify every unavailable message as a scam. This favors recall of possibly
important messages but leaves a weakness: an unavailable scam may wait for
review instead of being muted. That limitation is visible in the degraded
records.

## 8. Invariants and Anti-Patching Principles

An invariant is a rule the pipeline must preserve for every row, regardless of
whether the resulting prediction is good or bad.

| Invariant | Why it exists | Enforcement and test evidence |
|---|---|---|
| No hardcoded expected labels | Prevents the router from copying solved examples or evaluator outcomes. | `notification_router/inputs.py::SanitizedMessage` and `notification_router/evaluation.py::router_inputs` project only 11 input fields. Test `tests/test_milestone2.py::MilestoneTwoTests.test_sanitized_router_inputs_have_exactly_eleven_columns`. |
| No message-ID-specific fixes | Prevents case patches that memorize individual rows. | Routing modules have no message-ID decision table; behavior is based on schemas, context, features, and provider output. The generic 20-row run in `tests/test_milestone4a.py::MilestoneFourATests.test_fake_baseline_is_label_isolated_and_writes_immutable_bundle` is regression evidence, while source inspection is the direct check. |
| No target-label access | Keeps predictions independent from expected outcomes. | `notification_router/baseline.py::run_partition_baseline` builds label-free rows and computes evaluator metrics only after the pipeline. Test `tests/test_milestone4a.py::MilestoneFourATests.test_fake_baseline_is_label_isolated_and_writes_immutable_bundle`. |
| Holdout isolation | Prevents tuning against the sealed sample. | `notification_router/evaluation.py::EvaluationHarness.router_inputs` raises `HoldoutSealedError` for holdout access without reveal. Test `tests/test_milestone2.py::MilestoneTwoTests.test_split_is_deterministic_stratified_and_holdout_is_sealed`. |
| Same-user evidence only | Prevents cross-user contamination. | `notification_router/retrieval.py::validate_evidence_allowlist` checks candidate and history user IDs. Test `tests/test_milestone2.py::MilestoneTwoTests.test_retrieval_excludes_same_time_future_and_cross_user_rows`. |
| Strictly prior evidence only | Prevents future-data and same-time leakage. | `notification_router/models.py::NormalizedDataset.strictly_prior_history` uses `<`; retrieval validates it again. Tests `tests/test_milestone1.py::MilestoneOneTests.test_strict_temporal_filter_excludes_same_time_future_and_cross_user` and the corresponding milestone 2 test. |
| Evidence must come from the allowlist | Prevents arbitrary model-selected IDs. | `notification_router/contracts.py::parse_routing_decision`, `notification_router/retrieval.py::validate_selected_evidence`, and `notification_router/finalization.py::validate_routing_safety`. Test `tests/test_milestone2.py::MilestoneTwoTests.test_evidence_allowlist_and_selected_evidence_fail_closed`. |
| Raw model attempts preserved before parsing | Keeps malformed output diagnosable. | `notification_router/integration.py::ModelIntegrationClient._invoke` calls the raw response sink before `parser`. Test `tests/test_milestone3c.py::MilestoneThreeCTests.test_raw_response_sink_writes_before_invalid_output_validation`. |
| Validators are not weakened to improve metrics | Preserves safety and reproducibility over a better-looking score. | Strict parser and safety checks remain active; Candidate V2 was rejected by predefined gates rather than changing validators after results. Tests `tests/test_milestone3a.py::MilestoneThreeATests.test_routing_parser_rejects_extra_confidence_and_bad_evidence` and `tests/test_milestone3a.py::MilestoneThreeATests.test_semantic_support_bounds_are_strict_and_machine_readable`. |
| Completed runs are write-once | Prevents artifacts from being silently replaced. | `notification_router/artifacts.py::ImmutableArtifactStore.write_bytes` uses exclusive creation. Tests `tests/test_milestone2.py::MilestoneTwoTests.test_manifests_and_raw_predictions_are_write_once` and `tests/test_milestone4a.py::MilestoneFourATests.test_baseline_run_nonce_preserves_write_once_and_separates_runs`. |
| Final CSV is written only after complete validation | Prevents partial or invalid submissions. | `notification_router/target.py::run_target_submission` validates predictions before writing, then reparses and validates the output. Test `tests/test_submission.py::SubmissionTests.test_exact_output_is_atomic_and_reparseable`. |

The anti-patching principle also explains why the system does not silently
change a model response. A malformed response can be inconvenient, but quietly
repairing it would make the recorded result different from the provider result
and could turn an unsafe action into an apparently valid one.

## 9. Architecture Decisions and Tradeoffs

### 1. One structured routing model instead of many specialized agents

- **Chosen approach:** One `TextRoutingProvider` call returns the action, type,
  reason, evidence selection, semantic flags, and uncertainty.
- **Alternative considered:** Separate agents for urgency, spam, promotions,
  and evidence.
- **Reason:** One contract makes the finalization boundary clear and limits
  orchestration complexity.
- **Benefit:** Fewer moving parts, one packet, one validator, and simpler raw
  attempt accounting.
- **Cost:** A single model can confuse message type distinctions. The holdout
  type macro-F1 of 0.3333 shows this is a real limitation.
- **Source:** `notification_router/providers.py::TextRoutingProvider` and
  `notification_router/contracts.py::RawRoutingDecision`.

### 2. Separate media extraction and text routing stages

- **Chosen approach:** Media is extracted first, then its bounded record is put
  into a text routing packet.
- **Alternative considered:** One call that receives every media byte and makes
  the final decision directly.
- **Reason:** Extraction and routing have different schemas, retries, cache
  identity, and failure semantics.
- **Benefit:** Media results can be cached and inspected independently; the
  route contract stays stable.
- **Cost:** Extra provider work, latency, and another failure boundary.
- **Source:** `notification_router/providers.py::MultimodalExtractionProvider`,
  `notification_router/providers.py::TextRoutingProvider`, and `notification_router/integration.py`.

### 3. Deterministic retrieval instead of embeddings

- **Chosen approach:** Same-user, prior-only relationship, recency, and event
  scoring with stable top-K ordering.
- **Alternative considered:** Embedding search over historical messages.
- **Reason:** The first baseline needed deterministic provenance and no vector
  index or embedding version.
- **Benefit:** Easy leakage tests, reproducible order, and clear evidence origin.
- **Cost:** Less semantic recall for differently worded but relevant history.
- **Source:** `notification_router/retrieval.py::RetrievalConfig` and
  `retrieve_history`.

### 4. Strict schema-constrained output

- **Chosen approach:** Gemini JSON Schema plus strict Python parsing with exact
  keys, enums, bounds, and no duplicate JSON keys.
- **Alternative considered:** Parse free-form text or repair approximate JSON.
- **Reason:** The evaluator needs a stable row contract and safety needs known
  fields.
- **Benefit:** Invalid outputs are detectable and retries can be specific.
- **Cost:** Valid-looking but incomplete or unusual model responses are rejected.
- **Source:** `notification_router/contracts.py::routing_response_schema` and `notification_router/contracts.py::parse_routing_decision`.

### 5. Deterministic final confidence instead of trusting model confidence

- **Chosen approach:** Accept model `routing_uncertainty`, then calculate final
  confidence with a versioned formula and caps.
- **Alternative considered:** Ask Gemini for a confidence number and copy it.
- **Reason:** A raw number is not enough to account for media failure, evidence,
  context completeness, safety contradictions, or retries.
- **Benefit:** Every confidence can be audited and bounded consistently.
- **Cost:** `confidence-policy-provisional-v0` is not label-fitted calibration;
  the Brier and ECE results are observations, not tuning inputs.
- **Source:** `notification_router/confidence.py::calculate_final_confidence`.

### 6. Fail-closed fallback instead of silent repair

- **Chosen approach:** Reject invalid output, preserve the raw attempt, retry
  within bounds, then emit a visible degraded digest/unknown fallback.
- **Alternative considered:** Fill missing fields, drop invalid evidence, or
  rewrite the model response silently.
- **Reason:** Silent repair hides the cause and can alter safety meaning.
- **Benefit:** Failure is visible, bounded, and contract-valid.
- **Cost:** Six target rows lost normal semantic routing and were degraded.
- **Source:** `notification_router/integration.py::_invoke` and `notification_router/finalization.py::degraded_final_decision`.

### 7. Serialized or bounded provider calls instead of maximum throughput

- **Chosen approach:** The submitted configuration uses concurrency 1 by
  default, one retry by default, timeout and total/per-call cost ceilings, with
  bounded concurrency available in `IntegrationConfig`.
- **Alternative considered:** Maximum parallelism and unlimited retries.
- **Reason:** Spend control, deterministic artifacts, and provider stability were
  more important than throughput for the first backend baseline.
- **Benefit:** Easy accounting and fewer rate spikes.
- **Cost:** Target mean latency was about 20.8 seconds per row, with p95 about
  44.1 seconds; it is not a low-latency live service.
- **Source:** `notification_router/config.py::IntegrationConfig` and `notification_router/integration.py::CostLedger`.

### 8. Content-addressed caching

- **Chosen approach:** Cache successful extraction by media content and semantic
  configuration identity, using immutable files.
- **Alternative considered:** Cache by message ID or file path only.
- **Reason:** The same bytes can appear under different paths, while a model,
  schema, or extractor change can make old output unsafe to reuse.
- **Benefit:** Lower repeated extraction cost without hiding configuration drift.
- **Cost:** Cache retention, encryption, and invalidation policy are still
  operational decisions; only successful extraction is reused.
- **Source:** `notification_router/extraction_cache.py::ExtractionCacheIdentity`.

### 9. Backend-first delivery instead of MV3 or UI

- **Chosen approach:** Submit a runnable Python backend, CLI entry points, tests,
  artifacts, and CSV output.
- **Alternative considered:** Build a browser extension, UI, or mobile package
  first.
- **Reason:** The challenge contract evaluates routing and output, so the core
  decision boundary was the highest-value first deliverable.
- **Benefit:** The backend can be tested independently of browser permissions
  and live WhatsApp state.
- **Cost:** There is no live user-facing integration. MV3, APK, UI, and live
  WhatsApp reading are not implemented.
- **Source:** entry points in `main.py`, `notification_router/main.py`,
  `notification_router/baseline.py`, and `notification_router/target.py`.

### 10. Frozen holdout instead of repeated tuning

- **Chosen approach:** Run the sealed holdout once after the development
  configuration was frozen and keep Candidate V1 final.
- **Alternative considered:** Repeatedly change prompts or rules after seeing
  holdout results.
- **Reason:** Repeated tuning would turn the holdout into another development
  set and make the result less credible.
- **Benefit:** The 0.4000 holdout action accuracy remains an honest limitation.
- **Cost:** Known errors were not optimized away after the holdout.
- **Source:** `notification_router/evaluation.py::EvaluationHarness` and the frozen metrics in
  `FINAL_REPORT.md`.

## 10. Evaluation Story

The numbers below are Candidate V1 records. Development, holdout, target, and
offline tests are separate evidence types.

### What each metric means

- **Accuracy:** Fraction of rows where one field, such as action, exactly
  matches the evaluator label.
- **Macro-F1:** Average of per-class F1 scores. It gives each action or message
  type equal weight even when classes have different counts.
- **Joint accuracy:** Fraction of rows where both action and message type are
  correct together.
- **Schema-valid rate:** Fraction of model or final rows satisfying the declared
  field contract. It does not mean the semantic answer is correct.
- **Evidence-valid rate:** Fraction whose selected IDs are inside the
  deterministic allowlist and preserve its order. It does not measure semantic
  evidence relevance.
- **Brier score:** Mean squared error between confidence and whether the action
  was correct. Lower is better.
- **ECE:** Expected calibration error, the weighted gap between average
  confidence and actual correctness across confidence bins. Lower is better.
- **Latency:** Time spent in recorded provider operations, reported as mean,
  p50, and p95 in milliseconds.
- **Degraded rows:** Rows that reached a final contract-valid fallback after a
  strict route or safety failure. They are not successful semantic predictions.
- **Retries:** Extra provider attempts beyond the first attempt.
- **Cache hits:** Successful extraction results reused from the content-addressed
  cache without a new extraction call.

### Development baseline

The frozen 20-row development baseline completed without degraded rows.

| Metric | Candidate V1 result |
|---|---:|
| Completed / failed / degraded | 20 / 0 / 0 |
| Action accuracy / macro-F1 | 0.8000 / 0.7985 |
| Message-type accuracy / macro-F1 | 0.7500 / 0.4688 |
| Joint action-type accuracy | 0.6000 |
| Raw model schema-valid rate | 1.0000 |
| Final schema-valid rate | 1.0000 |
| Evidence allowlist-valid rate | 1.0000 |
| Evidence exact-set precision / recall / F1 | 0.1930 / 0.5238 / 0.2821 |
| Raw confidence mean / Brier / ECE | 0.74125 / 0.16122 / 0.11693 |
| Mean / p50 / p95 latency | 13,303.63 / 13,608.63 / 21,607.74 ms |
| Tokens / retries / recorded cost | 112,600 / 0 / USD 0.00 |
| Extraction states | 13 not applicable, 7 ok |
| Extraction cache | 7 hits, 0 misses, 0 corrupt |
| Error records | 0 |

The evidence result is an important honest detail: all selected IDs were
provenance-valid, but exact evidence overlap was modest. This is why "valid
evidence" must not be presented as "all evidence was relevant."

### Sealed holdout

The sealed 10-row holdout was executed once after the development configuration
was frozen. The router received label-free inputs during the run; evaluation
labels were used only after the pipeline completed.

| Metric | Candidate V1 result |
|---|---:|
| Completed / failed / degraded | 10 / 1 / 1 |
| Action accuracy / macro-F1 | 0.4000 / 0.2952 |
| Message-type accuracy / macro-F1 | 0.5000 / 0.3333 |
| Joint action-type accuracy | 0.2000 |
| Raw model schema-valid rate | 0.9000 |
| Final schema-valid rate | 1.0000 |
| Evidence allowlist-valid rate | 1.0000 |
| Evidence exact-set precision / recall / F1 | 0.3333 / 0.7000 / 0.4516 |
| Raw confidence mean / Brier / ECE | 0.68204 / 0.28188 / 0.28204 |
| Mean / p50 / p95 latency | 17,663.18 / 15,034.13 / 30,712.10 ms |
| Tokens / retries / recorded cost | 67,619 / 1 / USD 0.00 |
| Extraction states | 9 not applicable, 1 ok |
| Extraction cache | 0 hits, 1 miss, 0 corrupt |
| Error taxonomy | S7: 1 `SEMANTIC_FLAG_CONTRADICTION` |

Development performance was stronger than holdout performance. The holdout was
not used for further tuning. That is a weakness, but it is also the reason the
holdout number is useful evidence rather than a hidden optimization target.

### Target execution and submitted output

The target run processed all 110 rows. Six rows used the explicit degraded
fallback. The target runner still produced one contract-valid final decision for
every row.

| Metric | Candidate V1 result |
|---|---:|
| Completed / failed / degraded | 110 / 6 / 6 |
| Raw model rows / raw schema-valid rate | 104 / 0.94545 |
| Final output schema-valid rate | 1.0000 |
| Evidence allowlist-valid rate | 1.0000 |
| Missing / extra / duplicate output rows | 0 / 0 / 0 |
| Selected evidence IDs / valid selected-ID rate | 287 / 1.0000 |
| Extraction states | 87 not applicable, 23 ok |
| Extraction cache | 7 hits, 16 misses, 0 corrupt |
| Mean / p50 / p95 latency | 20,807.20 / 18,105.75 / 44,148.14 ms |
| Tokens / retries / recorded cost | 795,587 / 18 / USD 0.00 |
| Error taxonomy | 2 `ROUTER_SCHEMA_INVALID`, 2 `ROUTER_RATE_LIMITED`, 1 `SEMANTIC_FLAG_CONTRADICTION`, 1 `ACTION_CONSTRAINT_VIOLATION` |

The target accounting used 385,452 input tokens and 57,581 output tokens. The
recorded cost is USD 0.00 because the local token-price configuration was zero;
the runner did not query billing. It is inaccurate to call this verified zero
real cost. The project report gives a conservative published-rate estimate
below USD 0.59, but actual billing was not queried.

The inspected `dataset/output.csv` has the exact header
`message_id,action,message_type,reason,confidence,evidence_message_ids`, 110
rows, no duplicate IDs, valid action and type domains, confidence values in
range, and 287 selected evidence IDs. It contains 45 `notify`, 22 `digest`, and
43 `mute` rows. The output SHA-256 is
`0669e21406528c70d4b21d8971a0591d65c0587a0b9954bc2abcd5b0a87c069a`.
Those checks establish output validity and aggregate properties, not semantic
correctness for each message.

### Offline test suite

The verified report records 55 tests passed. The tests are valuable because they
exercise the boundaries without a live provider: exact schemas, label isolation,
temporal retrieval, evidence provenance, structured parsing, mocked Gemini
backends, retries, cache identity, immutable artifacts, confidence, and atomic
output validation. Tests do not convert the holdout or target into development
data.

## 11. Debugging Story

### Problem

The original development run failed systematically. The shorthand `20/20/20`
meant 20 rows entered the runner, 20 rows had stage errors, and all 20 became
degraded fallbacks. Seven media rows failed at extraction, and the downstream
router-unavailable path affected all 20 rows.

### Evidence

Preserved provider diagnostics showed a Vertex `404 NOT_FOUND` before response
generation. The configured publisher model `gemini-3.1-flash-lite` was
unavailable in the selected location. The run artifacts also preserved the stage,
attempt, error code, and bounded diagnostic rather than only the final CSV.

### Root cause

The root cause was model and location availability, not a message-specific
semantic pattern. The phrase "preexisting local files" described repository
status observed by a safety check. It was not the model service failure and did
not explain the Vertex 404.

### Generalized fix

The repair used explicit model and backend configuration, stronger timestamp and
semantic-span contracts, corrected routing field instructions, machine-readable
validation feedback, safer SDK diagnostics, and a target runner with an exact
output contract. The implementation normalizes provider errors in
`notification_router/gemini.py::_provider_call_error` and retries or
fallbacks through `notification_router/integration.py::_invoke`.

### Regression protection

No message-specific rule was added. Mocked provider tests verify structured
schemas and retries, while `tests/test_milestone3b.py::MilestoneThreeBTests.test_sdk_failure_diagnostics_keep_status_and_redact_sensitive_text`
checks that status survives without exposing sensitive diagnostic text. The
write-once artifact and raw-attempt tests preserve a future audit trail.

### Result

A fresh immutable V1 development run using the explicitly selected Vertex
`gemini-2.5-flash` configuration completed all 20 rows with zero failed or
degraded rows. The fresh run's action accuracy was 0.8000. The fix was
configuration and diagnostics generalization, not a change targeted at a
particular message.

## 12. Candidate V2 Experiment

Candidate V2 was an experiment only. Candidate V1 was backed up before the
attempt, and the backup record preserved the V1 code and output hashes. One
controlled V2 development run was evaluated against the predefined gates. The
submitted system remained Candidate V1. Target output was not regenerated with
the rejected candidate.

The exact comparison below is recorded in
`.artifacts/candidate-v2-dev-report-20260802-143744494/candidate-v2-comparison.json`.
The V1 backup identity is recorded in
`.artifacts/candidate-v1-backup-20260802-143155950/manifest.json`. These are
verification artifacts for the experiment, not part of the submitted code
package and not an indication that V2 was submitted.

| Development metric | V1 | V2 | Change |
|---|---:|---:|---:|
| Action accuracy | 0.8000 | 0.7000 | -0.1000 |
| Action macro-F1 | 0.7985 | 0.6778 | -0.1208 |
| Notify recall | 0.8333 | 0.3333 | -0.5000 |
| Message-type accuracy | 0.7500 | 0.8000 | +0.0500 |
| Message-type macro-F1 | 0.4688 | 0.5234 | +0.0545 |
| Joint action-type accuracy | 0.6000 | 0.6500 | +0.0500 |
| Raw model schema-valid rate | 1.0000 | 0.9500 | -0.0500 |
| Final schema-valid rate | 1.0000 | 1.0000 | unchanged |
| Evidence-valid rate | 1.0000 | 1.0000 | unchanged |
| Degraded rows | 0 | 1 | +1 |
| Brier / ECE | 0.1612 / 0.1169 | 0.2129 / 0.1720 | worse |

V2 failed its gates because degraded rows increased, action macro-F1 fell by
more than the allowed 0.03, notify recall fell by 0.50, and one routing rate
limit produced a degraded row. V2 did improve message-type accuracy, type
macro-F1, and joint accuracy, so the decision was not based on hiding every
positive number. The more important action-quality and reliability criteria
failed. V1 remained final.

## 13. Repository Map

The following tree contains only files verified in the submitted archive. Paths
are relative to the root of `code.zip`.

```text
code.zip members/
|-- main.py
|-- pyproject.toml
|-- requirements.txt
|-- .env.example
|-- README.md
|-- FINAL_REPORT.md
|-- REPRODUCIBILITY.md
|-- evaluation/
|   `-- main.py
|-- notification_router/
|   |-- __init__.py
|   |-- artifacts.py
|   |-- baseline.py
|   |-- confidence.py
|   |-- config.py
|   |-- contracts.py
|   |-- dataset.py
|   |-- errors.py
|   |-- evaluation.py
|   |-- extraction_cache.py
|   |-- features.py
|   |-- finalization.py
|   |-- gemini.py
|   |-- inputs.py
|   |-- integration.py
|   |-- main.py
|   |-- media.py
|   |-- metrics.py
|   |-- models.py
|   |-- packet.py
|   |-- predictions.py
|   |-- providers.py
|   |-- retrieval.py
|   |-- schemas.py
|   |-- smoke.py
|   |-- submission.py
|   |-- target.py
|   `-- telemetry.py
`-- tests/
    |-- test_milestone1.py
    |-- test_milestone2.py
    |-- test_milestone3a.py
    |-- test_milestone3b.py
    |-- test_milestone3c.py
    |-- test_milestone4a.py
    `-- test_submission.py
```

### File roles

- `main.py` is the standalone diagnostic entry point. It validates and inspects
  the dataset but intentionally writes no predictions.
- `notification_router/main.py` implements the diagnostic CLI.
- `notification_router/schemas.py`, `notification_router/errors.py`,
  `notification_router/models.py`, `notification_router/inputs.py`, and
  `notification_router/dataset.py` define and enforce input contracts, typed rows, joins, and the
  label-free 11-column boundary.
- `notification_router/retrieval.py` implements prior-only retrieval and evidence provenance.
- `notification_router/features.py` computes deterministic features and action constraints.
- `notification_router/packet.py` constructs and validates the canonical routing packet.
- `notification_router/providers.py` defines provider protocols, offline fakes, and the generic
  HTTP adapter.
- `notification_router/gemini.py` is the explicit Google Gemini Vertex or AI Studio adapter.
- `notification_router/contracts.py` defines extraction and routing JSON schemas and strict parsers.
- `notification_router/integration.py` owns retries, machine feedback, cost limits, timing, and raw
  response capture.
- `notification_router/confidence.py` and `notification_router/finalization.py` implement final confidence, safety
  checks, and degraded fallback behavior.
- `notification_router/extraction_cache.py` implements content-addressed write-once extraction
  caching.
- `notification_router/artifacts.py` and `notification_router/telemetry.py` implement immutable artifacts and redacted
  accounting.
- `notification_router/baseline.py` runs development, explicit holdout, and label-free target
  partitions. `notification_router/target.py` is the final output entry point.
- `notification_router/submission.py` validates and atomically writes the exact CSV contract.
- `notification_router/evaluation.py` owns the deterministic sample split and evaluator-side
  metrics. `metrics.py` calculates field, evidence, confidence, and operations
  metrics.
- `smoke.py` runs at most one development text, image, and voice sample using
  fake or explicitly enabled providers.
- `pyproject.toml` exposes the diagnostic, smoke, baseline, and target commands;
  `requirements.txt` declares the official Google Gen AI SDK.
- `.env.example` shows placeholder configuration and keeps live API access
  disabled by default. It contains no real secret values.
- `README.md`, `FINAL_REPORT.md`, and `REPRODUCIBILITY.md` document scope,
  commands, boundaries, and verified results.
- `evaluation/main.py` exists in the archive but contains no implementation;
  the functional evaluator is `notification_router/evaluation.py`.

## 14. How AI Was Used During Development

The honest interview answer is that AI assistance was used as an engineering
accelerator, while the owner retained the product and acceptance decisions.
The owner does not need to claim that every line was typed or memorized by hand.

### Human or owner decisions

- Define the notification problem and the three actions.
- Set the architecture boundary between semantic model work and deterministic
  validation, evidence, safety, confidence, and output.
- Require multimodal input support and explicit failure visibility.
- Require label isolation, same-user prior-only evidence, and no case-specific
  patches.
- Decide that the holdout would be frozen and used once.
- Set the acceptance and rejection gates for candidate experiments.
- Accept Candidate V1 as final after reviewing its metrics, degraded rows, and
  artifacts.

### AI-assisted work

- Generate implementation suggestions and Python modules.
- Draft and expand offline tests around the contracts.
- Suggest generalized debugging changes after the provider failure.
- Help execute structured development experiments and summarize metrics.
- Draft documentation and interview explanations.

### Verification of generated work

Generated work was checked against the source and tests rather than accepted on
appearance. Verification included:

- 55 recorded offline tests covering contracts, provenance, mocked providers,
  caching, artifacts, confidence, and output validation.
- Direct source inspection of `notification_router/schemas.py`,
  `notification_router/dataset.py`, `notification_router/retrieval.py`,
  `notification_router/packet.py`, `notification_router/contracts.py`,
  `notification_router/gemini.py`, `notification_router/integration.py`,
  `notification_router/finalization.py`, `notification_router/baseline.py`,
  `notification_router/target.py`, and `notification_router/submission.py`.
- Preserved raw attempts, packet hashes, per-row records, error codes, and
  metrics for the development, holdout, and target runs.
- Generalized regression checks for model availability diagnostics, retries,
  semantic span bounds, evidence allowlists, and write-once artifacts.
- A controlled Candidate V2 comparison with predefined gates. V2 was rejected
  even though some type metrics improved because action macro-F1 and notify
  recall deteriorated and degraded rows increased.

## 15. Likely AI Judge Questions

The sample answers below are deliberately direct. They are designed for spoken
answers of roughly 30 to 90 seconds, not for reading every implementation line
aloud.

### Product and problem

**1. What problem does this system solve?**

It reduces notification overload by deciding whether each message should
interrupt now, wait in a digest, or be muted. The product problem is not merely
classification. It is the cost of interrupting the wrong person and the cost of
missing an important message. The system combines content and receiver context,
then makes the output explicit in `notify`, `digest`, or `mute`. The action
contract is defined in `notification_router/schemas.py`.

**2. Why are three actions better than a binary important or unimportant label?**

A binary label cannot distinguish “useful but not urgent” from “do not show this
at all.” `digest` preserves useful information without immediate interruption,
while `mute` suppresses low-value or risky content. `notification_router/finalization.py` also lets
the system keep safety constraints separate from urgency. This is a product
choice reflected directly in `ACTION_VALUES` and the final CSV contract.

**3. What was actually submitted?**

The submission is a Python backend package, not a live WhatsApp product. It has
CSV validation, normalized context, deterministic retrieval, multimodal Gemini
adapters, structured routing, safety checks, caching, immutable artifacts,
offline tests, and a target output writer. It includes a validated 110-row
`output.csv`. It does not include a browser extension, UI, mobile APK, or live
WhatsApp adapter. The entry points are `notification_router/main.py`,
`notification_router/baseline.py`, and `notification_router/target.py`.

### Architecture

**4. Why is this architecture more than a wrapper around Gemini?**

Gemini only proposes semantic extraction and a structured route. Python owns the
input contract, joins, timestamp policy, same-user prior-only retrieval, packet
label isolation, evidence allowlisting, semantic span validation, action
constraints, deterministic confidence, retries, raw-attempt preservation, and
atomic output. Those responsibilities are visible in `notification_router/dataset.py`, `notification_router/retrieval.py`,
`notification_router/packet.py`, `notification_router/integration.py`, `notification_router/finalization.py`, and `notification_router/submission.py`. A model
call is one stage inside a larger controlled system.

**5. Why use one structured routing model rather than several agents?**

The first version needed a stable contract and easy failure analysis. One route
response contains action, type, reason, evidence, uncertainty, and semantic
flags, so one validator can check the complete proposal. Multiple agents could
improve specialization but would introduce arbitration, more calls, more cost,
and more inconsistent failure modes. The tradeoff is that the single model
still confuses some message types, which is visible in holdout macro-F1 of
0.3333. See `notification_router/contracts.py::RawRoutingDecision`.

**6. Why separate media extraction from routing?**

Media extraction and notification routing have different inputs and failure
semantics. Extraction produces a request-bound `ExtractionRecord` that can be
cached and quality-capped. Routing consumes a bounded packet and produces a
decision. Separating them lets a missing or low-quality media result remain
visible instead of being confused with a successful route. The interfaces are
`notification_router/providers.py::MultimodalExtractionProvider` and
`notification_router/providers.py::TextRoutingProvider`.

**7. Why is provider concurrency bounded?**

The default `IntegrationConfig` uses concurrency 1, one retry, a timeout, and
cost ceilings. This favors reproducibility, spend control, and predictable
provider load over maximum throughput. The cost is latency: target p95 was
about 44.1 seconds. The code can configure bounded concurrency up to its stated
limit, but the submitted baseline was conservative. See `notification_router/config.py` and
`notification_router/integration.py::CostLedger`.

### Personalization

**8. How does the receiver affect the decision?**

The receiver selects the user context, history, events, do-not-disturb window,
and relationship rows. `notification_router/features.py` computes quiet-hours, group mute, prior
interaction, prior report, trust, and missing-context signals. Retrieval is also
restricted to that receiver. The model sees the resulting context in the packet,
but Python still enforces actions that are prohibited by quiet hours or group
mute. See `notification_router/packet.py::_user_context` and `notification_router/features.py::compute_deterministic_features`.

**9. Does the system know that a user likes one character or dislikes generic anime promotions?**

No. That preference is not in the submitted schema, features, or packet. The
system can use business promotion opt-out and prior interaction signals that are
actually present, but it cannot claim an onboarding preference that was never
implemented. A future preference store with consent and versioned packet fields
would be the honest extension. This distinction is important because
personalization should be evidence-based, not inferred from a product story.

**10. How would you explain a trusted promotion versus an unwanted promotion?**

The business path checks verification, domain consistency, user-business
history, promotion permission, and prior activity. Gemini interprets whether
the current content is promotional or transactional. If it is a useful
non-urgent promotion and there is no opt-out, `digest` may be appropriate. If a
prior opt-out exists and the content is non-transactional, `notification_router/finalization.py`
requires `mute`. The policy is deterministic, while the current content meaning
is model-driven.

### Multimodal processing

**11. Why not trust file extensions?**

Extensions are declarations, not proof. `notification_router/media.py::sniff_bytes` checks the
leading bytes for JPEG, PNG, WebP, AVIF, MP3, M4A, or WAV signatures. The result
records both detected and extension formats, so a mismatch is visible. This
prevents an arbitrary file or disguised content from being sent as the expected
media type. The behavior is covered by
`test_media_sniffing_uses_bytes_not_extensions`.

**12. What happens when image or voice extraction fails?**

Missing or unsupported media becomes a visible extraction state. Provider
failure becomes a low-quality `empty_extraction` record after bounded retries.
The route can still use message text where available, but confidence is reduced
or capped. If routing itself fails, the row becomes `digest, unknown` with a
low-confidence degraded marker. The system does not claim that a failed
extraction was semantically understood. See `notification_router/baseline.py` S3 handling and
`notification_router/confidence.py::_extraction_quality`.

### Retrieval and evidence

**13. Why should the judge trust the evidence IDs?**

Because the model does not get to define the evidence universe. `retrieve_history`
creates an allowlist from same-user history with strictly earlier timestamps.
`parse_routing_decision` accepts only IDs in that allowlist, and
`validate_selected_evidence` checks count, uniqueness, and order. The packet
validator checks the candidate source again. This proves provenance and
temporal eligibility. It does not prove that the model selected the most
semantically relevant eligible item, which is why exact evidence precision and
recall are reported separately.

**14. How do you prevent future-data leakage?**

The history view uses `created_at < incoming.created_at`, not less-than-or-equal,
and the retrieval validator repeats the check. It also requires the same user.
Tests add synthetic earlier, same-time, future, and cross-user rows and confirm
only the earlier same-user row can appear. The relevant functions are
`NormalizedDataset.strictly_prior_history` and `retrieval.validate_evidence_allowlist`.

**15. Why not use embeddings?**

Embeddings could improve semantic retrieval for paraphrased history, but they
would add an embedding model, index, versioning, storage, and another leakage
boundary. V1 selected deterministic relationship, recency, and behavior scoring
so every candidate could be explained and reproduced. The cost is weaker semantic
recall. The development evidence exact-set F1 of 0.2821 shows that provenance
validity is stronger than evidence matching quality. See `notification_router/retrieval.py`.

### Safety

**16. What happens when Gemini returns invalid JSON?**

The raw bytes are written before parsing. The strict parser rejects invalid JSON,
duplicate keys, missing fields, extra fields, bad enums, invalid support spans,
and unsupported evidence. The integration layer can retry with bounded,
machine-readable feedback. If the response remains invalid, the route becomes a
visible degraded fallback. It is not silently patched. See `notification_router/contracts.py`,
`notification_router/integration.py::_invoke`, and `tests/test_milestone3c.py::MilestoneThreeCTests.test_raw_response_sink_writes_before_invalid_output_validation`.

**17. Is a muted scam safer than notifying the user?**

When the current message has corroborated high-risk signals, the deterministic
safety rule requires `mute`. But when the provider is unavailable, V1 does not
pretend it knows a message is a scam. The generic fallback is `digest, unknown`
with low confidence so the message is queued for later review. That choice can
expose the user to a delayed scam and is a known weakness. It avoids the opposite
failure of silently suppressing an important message when semantic evidence is
missing. See `_high_risk_required_mute` and `degraded_final_decision`.

**18. How do you prevent a trusted flag from overriding safety?**

Trust is used as context and as a narrow negative control, not as an unconditional
permission. A high-risk request requires mute unless an explicit negative
control applies, and quiet-hours or muted-group notify constraints are checked
independently. Semantic flags must also have current-message support spans. The
logic is in `notification_router/finalization.py::validate_routing_safety`, with tests in
`test_quiet_hours_constraint_is_strict_and_trusted_flags_do_not_force_mute`.

### Evaluation

**19. Why did holdout accuracy fall?**

Development had 20 rows and action accuracy 0.8000. Holdout had 10 rows and
action accuracy 0.4000, macro-F1 0.2952, and joint accuracy 0.2000. That gap
means the model and provisional policy did not generalize reliably from the
small development sample. The holdout also had one semantic contradiction that
became a degraded row. The correct answer is to report the gap, not to describe
the development score as production performance. Metrics are calculated in
`metrics.py` and preserved in `FINAL_REPORT.md`.

**20. Why did you not tune after the holdout?**

Because the holdout was explicitly sealed and intended as a one-time evaluation.
Changing prompts or rules after seeing it would make it another development
set. V1 therefore remains the frozen submitted candidate, including its weak
holdout result. The boundary is implemented by `notification_router/evaluation.py::EvaluationHarness`
and verified by `test_split_is_deterministic_stratified_and_holdout_is_sealed`.

**21. What does degraded mean?**

Degraded means the normal semantic route or safety validation did not complete
successfully, so the system emitted the declared deterministic fallback. It does
not mean the fallback was a correct semantic prediction. In the target run six
rows were degraded, yet all 110 rows were schema-valid. Those two facts are not
contradictory: output validity measures format and provenance, while semantic
correctness requires labels. See `FinalDecision.degraded` and
`degraded_final_decision`.

**22. How is confidence calculated?**

`calculate_final_confidence` combines routing certainty, extraction quality,
evidence strength, context completeness, and semantic signal agreement with
weights 0.30, 0.20, 0.20, 0.20, and 0.10. It then applies penalties for missing
relationship support, undefended aggregate snapshots, contradictions, and
retries, plus caps for poor media and degraded rows. The output is clamped to
0.05 through 0.98 and rounded once. It is a provisional deterministic policy,
not a trained calibrator.

**23. What is the strongest measured result?**

The strongest honest result is development action accuracy 0.8000 and action
macro-F1 0.7985, with 1.0000 final schema validity and evidence provenance
validity. The important qualifier is that holdout action accuracy was 0.4000
and target had six degraded rows. I would present all three contexts because
the first shows the prototype can work, while the latter two show why it is not
production-ready.

### Debugging

**24. What was the most important debugging incident?**

The first development run failed systematically because the selected Vertex
publisher model returned 404 before generation. Preserved artifacts separated
seven media extraction failures from the downstream router-unavailable path.
The phrase “preexisting local files” was repository-status context, not the
provider root cause. The generalized fix selected an available explicit model,
improved diagnostics and contracts, and kept the raw attempts. A fresh immutable
V1 run then completed 20 development rows without degraded rows. See
`FINAL_REPORT.md` and `notification_router/gemini.py::_provider_call_error`.

**25. How do you know the fix was not a message-specific patch?**

The repair changed configuration handling, timestamp and span contracts,
validation feedback, SDK diagnostics, and output orchestration. It did not add a
branch on a message ID or a label. The same generic path processed every row,
and the source has no message-ID decision table. Write-once artifacts and
contract tests make the fix reviewable. The answer should acknowledge that no
automated test can prove the absence of every possible future hardcoded branch;
source inspection and the invariant are the direct controls.

### Tradeoffs

**26. Why is the fallback digest rather than mute?**

The fallback lacks sufficient semantic evidence to call every unavailable message
unsafe. `digest, unknown` preserves a possible urgent message for later review
and is explicitly marked degraded and low-confidence. This is not claimed as
ideal scam handling. A future policy could add a separate deterministic abuse
signal or quarantine queue, but that would require stronger evidence and tests.
The current behavior is in `notification_router/finalization.py::degraded_final_decision`.

**27. Why not build the MV3 extension?**

The challenge submission contract evaluates a runnable backend and CSV output.
The implementation prioritized validated routing, provenance, and reproducible
artifacts. There is no MV3 code, DOM integration, browser permission handling,
or live WhatsApp connection in the archive. With another phase, an adapter could
translate live events into the existing 11-field `SanitizedMessage`, call the
same packet pipeline, and render the validated action. It would be a separate
delivery layer, not a claim about V1.

**28. Why accept 20-second mean target latency?**

The first baseline used serialized or bounded calls, strict retries, and a
content cache. This made cost and artifact accounting easier, but it is too slow
for a user-facing interrupt path. The result is an architecture tradeoff, not a
performance claim. A next version could use bounded parallelism, streaming or
smaller models, media batching, and a latency budget, while preserving the same
contracts. The operational controls are in `notification_router/config.py` and `notification_router/integration.py`.

### AI-assisted development

**29. What part did you personally design?**

The owner should answer with decisions, not authorship theater: the product
actions, label-isolation boundary, same-user prior-only evidence rule, safety
expectations, no-case-patch rule, evaluation gates, and final V1 acceptance were
human decisions. AI helped generate and review implementation material, but the
owner chose what behavior was allowed to ship. Source, tests, artifacts, and
metrics are the evidence of verification.

**30. How did you verify AI-generated code?**

The code was checked by reading the actual modules, running or reviewing the
recorded offline test suite, examining preserved raw attempts and metrics, and
using negative controls for leakage, malformed output, evidence, retries, and
atomic writes. The provider adapter was mocked in tests. Candidate V2 also had
predefined rejection gates. I would not claim that AI-generated code is correct
because it looks plausible; I would point to the contracts and regression
evidence in `tests/`.

### Limitations and future work

**31. What is the biggest weakness of the submission?**

The biggest weakness is generalization and coverage. Holdout action accuracy was
0.4000, message-type macro-F1 was 0.3333, evidence exact-set matching was not
strong, and six target rows were degraded. The architecture protects the output
contract and provenance, but those protections do not create semantic accuracy.
The next improvements should target calibration, retrieval relevance, provider
reliability, and coverage of user preferences rather than claiming the current
score is enough.

**32. What would you redesign with one more week?**

I would keep the label-free packet and finalization boundaries, then add a
versioned preference profile with consent, embedding retrieval with strict
same-user and as-of filters, a calibrated confidence model trained only on
development data, and a separate quarantine path for high-risk uncertain
messages. I would also add a WhatsApp Web adapter and measure end-to-end latency.
Each addition would need new schemas, artifact fields, and regression tests.

**33. Is this production-ready?**

No. It is a disciplined backend prototype with clear contracts and a runnable
target path. It has no live WhatsApp adapter, has not been tested at production
throughput, has unverified real billing, uses provisional confidence, and had
six degraded target rows. The right claim is that it is evaluable and auditable
under the challenge contract, not that it is ready for real users.

## 16. Interview Danger Zone

The left column is a claim to avoid. The right column is a safer accurate answer.

| Do not say | Say instead |
|---|---|
| “The MV3 extension already exists.” | “The submitted system is backend-first. An MV3 adapter is future work.” |
| “The mobile APK is supported.” | “No APK is included. The package exposes Python CLI and CSV entry points.” |
| “It analyzes arbitrary documents and archives.” | “V1 recognizes JPEG, PNG, WebP, AVIF, MP3, M4A, and WAV signatures only.” |
| “It is production-ready.” | “It is a runnable, auditable prototype with explicit limitations.” |
| “Personalization is perfect.” | “Personalization uses provided context, but holdout performance shows limited generalization.” |
| “The six degraded rows were successful semantic predictions.” | “They were contract-valid degraded fallbacks, not successful semantic routes.” |
| “The real API cost was definitely zero.” | “Recorded cost was zero because local prices were configured as zero; billing was not queried.” |
| “The holdout result is strong.” | “Holdout action accuracy was 0.4000, lower than the 0.8000 development result.” |
| “Model confidence is trusted directly.” | “The model supplies routing uncertainty; final confidence is deterministic and audited.” |
| “Embeddings are implemented.” | “Retrieval is deterministic and non-embedding in V1.” |
| “The system reads live WhatsApp messages.” | “It reads the provided CSV and media files. A live adapter is future work.” |
| “The fallback always mutes scams.” | “High-risk valid proposals must mute, but provider-unavailable rows use digest/unknown fallback.” |
| “Every selected evidence item is semantically correct.” | “Every selected ID was provenance-valid; exact relevance overlap was weaker.” |
| “AI wrote the whole system, so I cannot explain it.” | “AI assisted implementation, while the owner can explain the contracts, decisions, tests, and gates.” |
| “The holdout was tuned until it improved.” | “The holdout was run once and not used for further tuning.” |

## 17. One-Page Cheat Sheet

### 30-second pitch

“This backend reduces WhatsApp notification overload by choosing `notify`,
`digest`, or `mute`. It handles text, image, and voice inputs, combines receiver,
sender, group, business, and prior interaction context, and uses Gemini for
semantic extraction and routing proposals. Deterministic Python owns schemas,
prior-only evidence, safety, confidence, retries, fallbacks, and atomic CSV
validation. V1 reached 0.8000 development action accuracy, but holdout accuracy
was 0.4000 and six target rows degraded. It is a backend prototype, not a live
WhatsApp extension.”

### Remember

- **Problem:** Too many interruptions cause missed important messages and
  notification fatigue.
- **Core architecture:** Validate -> normalize -> retrieve prior context ->
  extract media -> build packet -> Gemini proposal -> deterministic safety and
  confidence -> atomic output.
- **AI responsibility:** Understand media and message meaning; propose a
  structured decision.
- **Deterministic responsibility:** Enforce contracts, provenance, safety,
  confidence, retries, fallback, artifacts, and final CSV.
- **Three strongest engineering decisions:** Label isolation, same-user
  strictly-prior evidence allowlisting, and fail-closed immutable artifacts.
- **Three honest limitations:** Holdout action accuracy 0.4000, six degraded
  target rows, and no live WhatsApp Web adapter.
- **Key metrics:** Development action accuracy 0.8000, development action
  macro-F1 0.7985, holdout action accuracy 0.4000, target 110 rows, final
  schema validity 1.0000, target degraded rows 6.
- **Debugging story:** Vertex model availability caused a systematic 404; the
  generalized configuration and diagnostics fix produced a fresh clean V1 run.
- **Why no hardcoded patches:** They would memorize cases, leak labels, and make
  the score less defensible.
- **Future product path:** Feed live WhatsApp Web events into the existing
  label-free packet, then render only validated actions.

### Five phrases to remember

1. “The model proposes; deterministic Python enforces.”
2. “Valid provenance is not the same as semantic relevance.”
3. “Degraded means contract-valid fallback, not correct prediction.”
4. “The holdout was frozen and not tuned.”
5. “V1 is an auditable backend prototype, not production WhatsApp.”

## 18. Glossary for the Owner

- **Invariant:** A rule that must remain true for every row, such as “evidence
  must be same-user and strictly prior.”
- **Schema:** The exact field names, types, allowed values, and ordering expected
  for a CSV or JSON object.
- **Contract:** A promise between pipeline stages about what input and output
  shape is allowed. A contract is usually stricter than an informal schema.
- **Deterministic:** Given the same inputs and configuration, the result is the
  same. Sorting and safety checks in V1 are deterministic.
- **Multimodal extraction:** Turning non-text media such as an image or voice
  note into structured text and description fields.
- **Routing packet:** The canonical label-free JSON object sent to the routing
  provider. It contains the current message, safe context, features, constraints,
  candidates, and evidence allowlist.
- **Evidence allowlist:** The exact set of history IDs the model is permitted to
  select for one incoming message.
- **Provenance:** The traceable origin and eligibility of a value. For evidence,
  provenance means the ID came from allowed same-user prior history.
- **Temporal leakage:** Using information from the future, or from the same
  timestamp, to make an earlier decision.
- **Holdout:** A sealed evaluation subset used once after configuration is
  frozen, so its result is not optimized directly.
- **Macro-F1:** The average F1 score across classes, giving each class equal
  weight rather than weighting only by the most common class.
- **Brier score:** A squared-error measure of confidence against correctness.
  Lower values mean confidence is closer to the observed outcome.
- **ECE:** Expected calibration error. It measures the average gap between
  predicted confidence and actual correctness within confidence bins.
- **Degraded fallback:** A deterministic, low-confidence output used when the
  normal model or safety path cannot complete. It is visible and is not claimed
  as a semantic success.
- **Content-addressed cache:** A cache addressed by a hash of content and the
  semantic configuration needed to interpret it, not only by a filename.
- **Atomic write:** A file update that writes a complete temporary file, syncs it,
  then replaces the destination so readers do not see a partial CSV.
- **Bounded retry:** A retry policy with a maximum number of attempts, timeout,
  and cost ceiling. V1 defaults to one retry after the first attempt.
- **Fail closed:** When a safety or contract check cannot establish that output
  is acceptable, reject it or use the declared constrained fallback instead of
  guessing.
- **Regression test:** A test that protects a previously fixed behavior from
  breaking when code changes.
- **Vertex AI:** Google's managed cloud platform path used by the submitted
  Gemini adapter. V1 selected Vertex explicitly for its recorded final run.
- **Application Default Credentials:** A Google authentication mechanism that
  lets the runtime discover an authorized identity through the standard Google
  authentication chain. `notification_router/gemini.py` calls `google.auth.default` for Vertex ADC;
  the package does not include the identity material.

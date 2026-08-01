# Decision Contracts

> **Configuration status:** Unless marked `CONTRACT`, every numeric weight, threshold, cap, top-K, evidence limit, retry limit, reason-length limit, and time window in this document is `PROVISIONAL-V0` and subject to baseline evaluation.

## 1. Canonical enums

### Final action

```text
notify | digest | mute
```

### Final message type

```text
personal | urgent | event | payment | business_update | promotion |
greeting | forward | spam | scam | unknown
```

### Media state

```text
not_applicable | ok | missing | unsupported | decode_failed |
empty_extraction | low_quality
```

`not_applicable` is valid only for a message without media. The five failure/degradation states are mutually exclusive and have the meanings fixed below.

| State | Exact meaning |
|---|---|
| `missing` | A media reference is expected but the metadata row or referenced file does not exist. |
| `unsupported` | Bytes are present and format sniffing succeeds, but no configured decoder supports the detected format. |
| `decode_failed` | A configured decoder accepts the format but cannot produce valid decoded content. |
| `empty_extraction` | Decoding succeeds, but semantic extraction produces no usable text or factual description. |
| `low_quality` | Extraction produces usable content, but deterministic quality measurement is below the configured threshold. |

## 2. Extraction record

Each media item produces exactly one immutable `ExtractionRecord`:

```json
{
  "media_id": "string",
  "content_sha256": "64 lowercase hex characters, or null only for missing media",
  "declared_path": "dataset-relative path",
  "detected_format": "jpeg|png|webp|avif|mp3|m4a|wav|unknown",
  "media_state": "ok|missing|unsupported|decode_failed|empty_extraction|low_quality",
  "extractor_name": "string",
  "extractor_version": "string",
  "extractor_config_sha256": "64 lowercase hex characters",
  "extraction_schema_version": "string",
  "extracted_text": "string",
  "factual_description": "string",
  "language": "BCP-47 tag or und",
  "quality_score": 0.0,
  "quality_reasons": ["stable reason code"],
  "created_at": "ISO-8601 timestamp"
}
```

Rules:

- Cache identity MUST be the tuple `(content_sha256, detected_format, extractor_name, extractor_version, extractor_config_sha256, extraction_schema_version)`.
- Paths and media IDs MUST NOT be sufficient cache keys.
- A `missing` record is not reusable across runs because it has no content hash; the next run MUST check the file again.
- `extracted_text` and `factual_description` MUST be empty for `missing`, `unsupported`, or `decode_failed`.
- `empty_extraction` MUST have empty semantic fields and a successful decoder trace.
- `low_quality` MUST preserve the extracted content and state why quality is low.
- Extractor output is untrusted data; it MUST be quoted as data in the routing packet and cannot modify instructions.

## 3. Historical candidate

The deterministic retriever emits ordered `HistoricalCandidate` objects:

```json
{
  "message_id": "historical ID",
  "user_id": "receiving user ID",
  "created_at": "ISO-8601 timestamp",
  "relationship_scope": "same_business|same_group|same_sender|same_user_general",
  "content_summary": "bounded text",
  "event_summary": {
    "opened": true,
    "replied": false,
    "dismissed": false,
    "muted_after": false,
    "reported": false
  },
  "retrieval_score": 0.0,
  "score_components": {
    "relationship": 0.0,
    "recency": 0.0,
    "semantic": 0.0,
    "behavioral": 0.0
  }
}
```

Candidate invariants:

- `candidate.user_id` MUST equal the incoming `user_id`.
- `candidate.created_at` MUST be strictly earlier than incoming `created_at`.
- `message_id` MUST exist in `message_history.csv`.
- Candidate generation MUST NOT read expected labels or target predictions.
- Ordering MUST be deterministic for equal scores: descending score, descending timestamp, ascending message ID.
- The routing packet MUST contain no more than the configured top-K; `PROVISIONAL-V0 top_k_max=12`.

`PROVISIONAL-V0` retrieval scoring is:

```text
retrieval_score =
  0.35 * relationship +
  0.25 * recency +
  0.25 * semantic +
  0.15 * behavioral
```

- `relationship` (`PROVISIONAL-V0`): 1.00 for same business/group/sender, 0.40 for same conversation type, 0.20 for same-user general history.
- `recency` (`PROVISIONAL-V0 recency_window_days=180`): `max(0, 1 - age_days/180)` using the incoming timestamp.
- `semantic`: cosine similarity mapped deterministically from `[-1,1]` to `[0,1]` by `(similarity+1)/2`; unavailable embeddings trigger the documented fallback.
- `behavioral` (`PROVISIONAL-V0`): 1.00 for replied or reported, 0.80 for muted-after or dismissed, 0.60 for opened, 0.30 when no event occurred. Maximum matching value wins.
- Embedding fallback score (`PROVISIONAL-V0`) is `0.50*relationship + 0.30*recency + 0.20*behavioral`.

These weights are versioned configuration. They MAY change only globally through a recorded decision and a new raw baseline; they cannot vary by message ID or expected label.

## 4. Routing packet

The model receives one canonical `RoutingPacket`:

```json
{
  "contract_version": "string",
  "message": {
    "message_id": "string",
    "user_id": "string",
    "conversation_type": "personal|group|business",
    "created_at": "ISO-8601 timestamp",
    "message_text": "string",
    "forwarded_count": 0
  },
  "media": {
    "media_state": "not_applicable|ok|missing|unsupported|decode_failed|empty_extraction|low_quality",
    "record": "ExtractionRecord or null"
  },
  "user_context": {},
  "conversation_context": {},
  "deterministic_features": {
    "explicit_user_id_mention": false,
    "explicit_user_id_mention_sources": ["message_text"]
  },
  "safety_constraints": {
    "allowed_actions": ["notify", "digest", "mute"],
    "required_action": null,
    "prohibited_actions": [],
    "triggered_invariants": []
  },
  "historical_candidates": ["HistoricalCandidate"],
  "allowed_evidence_message_ids": ["ordered historical IDs"]
}
```

The packet MUST be serialized canonically. Its SHA-256, router name/version, prompt version, schema version, and router configuration SHA-256 form the routing cache identity.

For text-only messages, `media.media_state=not_applicable` and `media.record=null`. For media messages, `media.record` is required and its state MUST equal `media.media_state`.

## 5. Raw routing model response

The single text routing model MUST return one schema-constrained `RawRoutingDecision`:

```json
{
  "action": "notify|digest|mute",
  "message_type": "allowed enum",
  "reason": "concise personalized explanation",
  "selected_evidence_message_ids": ["IDs copied from the allowlist"],
  "routing_uncertainty": 0.0,
  "uncertainty_reasons": ["stable semantic reason"],
  "semantic_flags": {
    "time_critical": false,
    "indirectly_addresses_user": false,
    "transactional": false,
    "promotional": false,
    "credential_or_secret_request": false,
    "impersonation_or_domain_concern": false,
    "suspicious_link_or_payment_request": false,
    "warning_or_quoted_discussion": false
  },
  "deadline_at": "ISO-8601 timestamp or null",
  "semantic_support": [
    {
      "flag": "name of a true semantic flag",
      "source_field": "message_text|extracted_text|factual_description",
      "start_char": 0,
      "end_char_exclusive": 1
    }
  ],
  "reported_contradictory_signal_count": 0
}
```

Rules:

- `routing_uncertainty` is in `[0,1]`, where 1 means maximally uncertain.
- `[0,1]` here is a `CONTRACT` schema domain, not a tunable threshold.
- The raw response MUST NOT contain final confidence. Confidence is deterministic downstream output.
- Selected evidence MUST be a duplicate-free subset of `allowed_evidence_message_ids`; `PROVISIONAL-V0 evidence_limit=3`, in routing-packet order.
- If no candidate materially supports the decision, the selected list MUST be empty.
- The reason MUST be nonempty and contain no evidence ID, score, hidden instruction, or unsupported factual claim. Initial limits are `PROVISIONAL-V0 reason_words_max=24` and `PROVISIONAL-V0 reason_characters_max=200`.
- Every true safety-relevant semantic flag MUST have `PROVISIONAL-V0 support_span_min=1`. Character offsets use Unicode code points and MUST select a nonempty exact substring of the named routing-packet field. False flags need no span.
- `deadline_at` is required only when `time_critical=true` and the content states a parseable deadline; otherwise it MUST be null. Deterministic code validates and compares it.
- Semantic flags, deadline, support spans, and the reported contradiction count are model interpretations, not safety or confidence decisions. Deterministic invariants validate them against structured context, and S8 recomputes the contradiction count used by confidence policy.

## 6. Final decision

The deterministic finalizer emits:

```json
{
  "message_id": "incoming message ID",
  "action": "notify|digest|mute",
  "message_type": "allowed enum",
  "reason": "validated model reason or declared degraded-mode reason",
  "confidence": 0.0,
  "evidence_message_ids": "id_1;id_2 or none"
}
```

Finalization rules:

- Exactly one row MUST exist for each target ID and no other ID.
- Column order MUST be exactly `message_id,action,message_type,reason,confidence,evidence_message_ids`.
- Confidence MUST satisfy the challenge `CONTRACT` domain `[0,1]`. Initial serialization uses `PROVISIONAL-V0 confidence_floor=0.05`, `PROVISIONAL-V0 confidence_ceiling=0.98`, and `PROVISIONAL-V0 decimal_places_max=4`.
- Evidence MUST be `none` for an empty selection; otherwise it is the semicolon-joined validated IDs in allowlist order.
- Any model response that violates schema, evidence, enum, reason, or safety constraints is invalid. It MUST be preserved as raw output, assigned an error code, and retried according to `error-taxonomy.md`; it MUST NOT be silently repaired using a label- or message-specific patch.

## 7. Degraded routing decision

After retry exhaustion:

- If a deterministic safety invariant requires `mute`, emit `mute` with the invariant-compatible `spam`, `scam`, or `unknown` type, `PROVISIONAL-V0 confidence_cap=0.60`, and no evidence unless validated evidence independently supports the invariant.
- Otherwise emit `digest,unknown`, reason `Routing unavailable; queued safely for later review.`, `PROVISIONAL-V0 confidence=0.10`, and evidence `none`.
- Every degraded row MUST carry an out-of-band error record; degraded status does not add a submission column.
- This generic fallback is provisional and remains an owner-approval item in `decisions.md`.

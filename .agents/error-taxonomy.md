# Error Taxonomy

> **Configuration status:** Unless marked `CONTRACT`, every numeric retry limit, timeout, backoff, confidence fallback, and other operational threshold in this document is `PROVISIONAL-V0` and subject to baseline evaluation.

## Error record

Every error is an out-of-band structured record:

```json
{
  "run_id": "string",
  "message_id": "string or null",
  "stage": "S0-S9",
  "code": "stable code",
  "severity": "info|warning|error|fatal",
  "retryable": false,
  "attempt": 1,
  "fallback": "stable fallback code or none",
  "details": "bounded redacted details",
  "cause_hash": "sha256 of normalized cause"
}
```

Errors MUST NOT contain secrets or unrestricted message/media content.

## Codes and behavior

| Stage | Code | Severity | Retry | Exact behavior |
|---|---|---|---|---|
| S0 | `CFG_INVALID` | fatal | no | stop run |
| S0 | `INPUT_MISSING` | fatal | no | stop run |
| S0 | `CAPABILITY_MISSING` | error/fatal | no | continue only when S3 can represent `unsupported`; otherwise stop |
| S0 | `SECRET_MISSING` | fatal | no | stop before external call |
| S1 | `CSV_PARSE_FAILED` | fatal | no | stop run |
| S1 | `SCHEMA_MISSING_COLUMN` | fatal | no | stop run |
| S1 | `SCHEMA_EXTRA_COLUMN` | fatal | no | stop run unless evaluator explicitly strips known label columns first |
| S1 | `TYPE_INVALID` | fatal | no | stop run |
| S1 | `ENUM_INVALID` | fatal | no | stop run |
| S1 | `DUPLICATE_KEY` | fatal | no | stop run |
| S1 | `CONDITIONAL_FIELD_INVALID` | fatal | no | stop run |
| S2 | `REFERENCE_BROKEN` | fatal | no | stop run for required reference |
| S2 | `TIMESTAMP_INVALID` | fatal | no | stop affected run |
| S2 | `REQUIRED_JOIN_MISSING` | fatal | no | stop run |
| S2 | `OPTIONAL_CONTEXT_MISSING` | warning | no | continue as unknown; confidence impact |
| S3 | `MEDIA_MISSING` | warning | no | state=`missing`; route with explicit degradation |
| S3 | `MEDIA_UNSUPPORTED` | warning | no | state=`unsupported`; route with explicit degradation |
| S3 | `MEDIA_DECODE_FAILED` | error | no | state=`decode_failed`; route with explicit degradation |
| S3 | `MEDIA_EMPTY_EXTRACTION` | warning | no | state=`empty_extraction`; no invented content |
| S3 | `MEDIA_LOW_QUALITY` | warning | no | state=`low_quality`; keep content and apply cap |
| S3 | `EXTRACTOR_TIMEOUT` | error | yes | bounded retry; then `empty_extraction` if decode succeeded |
| S3 | `EXTRACTOR_RATE_LIMITED` | error | yes | bounded backoff; then `empty_extraction` |
| S3 | `EXTRACTOR_SCHEMA_INVALID` | error | yes | `PROVISIONAL-V0 schema_repair_limit=1`; then `empty_extraction` |
| S3 | `CACHE_CORRUPT` | warning | no | quarantine entry and recompute |
| S4 | `FEATURE_COMPUTE_FAILED` | error/fatal | no | optional feature becomes unknown; required feature stops run |
| S4 | `INVARIANT_CONFIG_INVALID` | fatal | no | stop run |
| S4 | `INVARIANT_CONTRADICTION` | error | yes | prohibit notify; bounded routing retry; then degraded policy |
| S5 | `RETRIEVER_FAILED` | error | no | use deterministic non-embedding fallback |
| S5 | `EMBEDDING_FAILED` | warning | no | use relationship/recency/behavior ranking |
| S5 | `NO_CANDIDATES` | info | no | empty allowlist; normal routing |
| S5 | `CANDIDATE_INVALID` | error | no | exclude candidate; fail stage if systematic |
| S6 | `PACKET_SCHEMA_INVALID` | error | no | degraded routing if not reconstructable |
| S6 | `PACKET_TOO_LARGE` | warning/error | no | deterministic truncation; degraded if minimal packet fails |
| S6 | `PACKET_SERIALIZE_FAILED` | error | no | degraded routing |
| S7 | `ROUTER_TIMEOUT` | error | yes | bounded retry; then degraded routing |
| S7 | `ROUTER_RATE_LIMITED` | error | yes | bounded backoff; then degraded routing |
| S7 | `ROUTER_UNAVAILABLE` | error | yes | bounded retry; then degraded routing |
| S7 | `ROUTER_SCHEMA_INVALID` | error | yes | `PROVISIONAL-V0 schema_repair_limit=1`; then degraded routing |
| S7 | `ROUTER_REFUSAL` | error | no | degraded routing |
| S7 | `ROUTER_CACHE_CORRUPT` | warning | no | quarantine and recompute |
| S8 | `ACTION_CONSTRAINT_VIOLATION` | error | yes | `PROVISIONAL-V0 constraint_retry_limit=1`; then degraded routing |
| S8 | `EVIDENCE_NOT_ALLOWED` | error | yes | reject, never silently drop; retry then degraded routing |
| S8 | `REASON_INVALID` | error | yes | `PROVISIONAL-V0 schema_repair_limit=1`; then degraded routing |
| S8 | `SEMANTIC_FLAG_CONTRADICTION` | error | yes | retry with contradiction context; then degraded routing |
| S8 | `CONFIDENCE_INPUT_INVALID` | error | no | valid decision gets `PROVISIONAL-V0 confidence=0.05` and critical audit record |
| S9 | `OUTPUT_ROW_COUNT_INVALID` | fatal | no | block publication |
| S9 | `OUTPUT_ID_SET_INVALID` | fatal | no | block publication |
| S9 | `OUTPUT_DUPLICATE_ID` | fatal | no | block publication |
| S9 | `OUTPUT_COLUMN_INVALID` | fatal | no | block publication |
| S9 | `OUTPUT_VALUE_INVALID` | fatal | no | block publication |
| S9 | `OUTPUT_WRITE_FAILED` | fatal | no | retain previous valid file |
| S9 | `BASELINE_MUTATION_ATTEMPT` | fatal | no | stop evaluation |

## Retry invariants

- Retry counts, timeouts, and backoff are configuration values and part of run identity.
- Retries receive the same semantic input and no expected-label feedback.
- Only schema/constraint error details required for repair may be added.
- Every attempt is retained; later success does not erase earlier failure.
- Retry exhaustion always enters a declared degraded behavior, never an ad hoc patch.

## Fallback classes

| Fallback | Meaning |
|---|---|
| `CONTEXT_UNKNOWN` | Continue with optional field absent and confidence penalty. |
| `MEDIA_DEGRADED` | Route with exact non-OK media state and available text/context. |
| `RETRIEVAL_NON_EMBEDDING` | Rank using deterministic relationship, recency, and behavior only. |
| `DEGRADED_SAFETY_MUTE` | A complete deterministic safety trigger survives router failure; emit bounded-confidence mute. |
| `DEGRADED_DIGEST_UNKNOWN` | No valid decision and no safety-required mute; emit provisional generic digest/unknown. |
| `RUN_ABORT` | Contract cannot produce a defensible complete output. |

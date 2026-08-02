# Architecture v0.1 submission report

## Scope and frozen boundaries

The submission implements the frozen Architecture v0.1 backend through target
output generation. It contains deterministic loading, joins, retrieval,
evidence allowlists, multimodal extraction, schema-constrained routing,
safety validation, deterministic finalization, caching, bounded Vertex AI
calls, immutable artifacts, and exact output validation.

It does not contain a UI, browser extension, APK, embeddings, OCR/ASR decoder,
prompt-tuning loop, label-derived message rules, final confidence calibration,
or holdout-driven changes. The router never receives expected labels.

## Baseline interpretation and diagnosis

The original `20/20/20` development report meant 20 rows entered the runner,
20 rows had stage errors, and all 20 rows were degraded fallbacks. `S3=7`
was the seven media rows that failed extraction. `S7=20` was the downstream
router-unavailable path for every row. The preserved provider diagnostics
showed a Vertex `404 NOT_FOUND` before response generation: the configured
`gemini-3.1-flash-lite` publisher model was unavailable in the selected
location. `preexisting-local-files` described repository status observed by
the safety check; it was not the provider root cause.

The generalized repairs were limited to explicit model/configuration
selection, strict timestamp and span contracts, corrected routing field
instructions, machine-readable validation feedback, safe SDK diagnostics, and
the target runner/output contract. No row-specific rule was added.

## Development baseline

The preserved fresh development run completed all 20 rows with no failed or
degraded rows. It used the explicitly selected Vertex `gemini-2.5-flash`
configuration and a separate immutable artifact namespace.

| Metric | Result |
| --- | ---: |
| Completed / failed / degraded | 20 / 0 / 0 |
| Raw model schema-valid rate | 1.0000 |
| Final schema-valid rate | 1.0000 |
| Evidence allowlist-valid rate | 1.0000 |
| Action accuracy / macro-F1 | 0.8000 / 0.7985 |
| Message-type accuracy / macro-F1 | 0.7500 / 0.4688 |
| Joint action-type accuracy | 0.6000 |
| Raw confidence mean / Brier / ECE | 0.74125 / 0.16122 / 0.11693 |
| Mean / p50 / p95 latency | 13,303.63 / 13,608.63 / 21,607.74 ms |
| Tokens / retries / recorded cost | 112,600 / 0 / USD 0.00 |
| Extraction states | 13 not_applicable, 7 ok |
| Extraction cache | 7 hits, 0 misses, 0 corrupt |
| Error records | 0 |

These are frozen baseline metrics, not tuning targets.

## Sealed holdout

The sealed holdout was executed exactly once after the development
configuration was frozen. Labels were accessed only by the evaluator after
the label-free pipeline completed; they were not written to router packets or
manifests.

The run produced 10 final decisions with valid final schema and evidence
provenance for every row. One model response triggered the existing strict
`SEMANTIC_FLAG_CONTRADICTION` safety invariant and became the declared
degraded fallback. This was an isolated safety outcome, not a systematic
provider or contract failure, and no holdout-based code change was made.

| Metric | Result |
| --- | ---: |
| Completed / failed / degraded | 10 / 1 / 1 |
| Raw model schema-valid rate | 0.9000 |
| Final schema-valid rate | 1.0000 |
| Evidence allowlist-valid rate | 1.0000 |
| Action accuracy / macro-F1 | 0.4000 / 0.2952 |
| Message-type accuracy / macro-F1 | 0.5000 / 0.3333 |
| Joint action-type accuracy | 0.2000 |
| Raw confidence mean / Brier / ECE | 0.68204 / 0.28188 / 0.28204 |
| Mean / p50 / p95 latency | 17,663.18 / 15,034.13 / 30,712.10 ms |
| Tokens / retries / recorded cost | 67,619 / 1 / USD 0.00 |
| Error taxonomy | S7: 1 `SEMANTIC_FLAG_CONTRADICTION` |

## Target execution and output

The target runner opened `messages.csv` only for this final prediction path.
It processed all 110 target rows, preserved raw attempts and per-row records,
and wrote `dataset/output.csv` only after final validation.

| Metric | Result |
| --- | ---: |
| Completed / failed / degraded | 110 / 6 / 6 |
| Raw model rows / raw schema-valid rate | 104 / 0.94545 |
| Final output schema-valid rate | 1.0000 |
| Evidence allowlist-valid rate | 1.0000 |
| Missing / extra / duplicate output rows | 0 / 0 / 0 |
| Selected evidence IDs / valid selected-ID rate | 287 / 1.0000 |
| Extraction states | 87 not_applicable, 23 ok |
| Extraction cache | 7 hits, 16 misses, 0 corrupt |
| Mean / p50 / p95 latency | 20,807.20 / 18,105.75 / 44,148.14 ms |
| Tokens / retries / recorded cost | 795,587 / 18 / USD 0.00 |
| Error taxonomy | S7: 2 `ROUTER_SCHEMA_INVALID`, 2 `ROUTER_RATE_LIMITED`, 1 `SEMANTIC_FLAG_CONTRADICTION`, 1 `ACTION_CONSTRAINT_VIOLATION` |

The six failed rows are counted as degraded because the strict router/safety
path emitted the generic fallback; they still have contract-valid final
decisions and are present in the exact 110-row output. No prediction-quality
repair was attempted after seeing these metrics.

The artifact cost ledger reports USD 0.00 because the local token-price
configuration is zero. The run used 385,452 input tokens and 57,581 output
tokens. As a conservative inference using the published Gemini 2.5 Flash
upper rates of USD 1.00 per million input tokens and USD 3.50 per million
thinking-output tokens, the same usage is below USD 0.59; actual billing was
not queried by the runner. See the [Vertex AI pricing page](https://cloud.google.com/vertex-ai/generative-ai/pricing)
for current rates.

The validated output artifact has 110 rows, the exact columns
`message_id,action,message_type,reason,confidence,evidence_message_ids`, and
SHA-256 `0669e21406528c70d4b21d8971a0591d65c0587a0b9954bc2abcd5b0a87c069a`.

## Verification

The complete offline suite passes:

```text
55 tests passed
```

The test suite covers dataset contracts, leakage boundaries, strict temporal
retrieval, evidence provenance, packet containment, fake providers, mocked
Gemini backends, raw-attempt preservation, cache identity, immutable runs,
final confidence, and atomic output validation.

Generated raw responses, media, ADC material, `.env`, caches, and logs remain
local and are not part of the repository or `code.zip`.

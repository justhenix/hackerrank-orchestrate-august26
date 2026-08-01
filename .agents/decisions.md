# Architecture Decision Record

## ADR-001: Routing architecture

- **Status:** selected by project owner
- **Decision date:** 2026-08-01
- **Decision owner:** project owner
- **Specification maturity:** Architecture v0.1, frozen
- **Specification role:** Codex formalized and consistency-checked the selected design; it did not select the architecture in this phase.

### Architecture v0.1 freeze record

The current `.agents/` specification set is frozen as Architecture v0.1 on
2026-08-01. The freeze covers the selected architecture, stage boundaries,
deterministic responsibilities, data contracts, safety invariants, error
taxonomy, evaluation protocol, and generalized regressions. `PROVISIONAL-V0`
values remain explicitly provisional until the separate configuration freeze.

### Context

The system must route text, image, and voice messages while preserving deterministic data handling, evidence provenance, temporal discipline, safety boundaries, confidence adjustment, and output validation. Media failures must remain distinguishable from routing failures. Evaluation must preserve raw, unpatched results.

### Alternatives considered

#### Option A: local-first deterministic retrieval plus interpretable scorer

Local OCR/ASR feeds deterministic features, retrieval, and an interpretable classifier or points model.

- Strengths: low API cost, offline operation, reproducibility, auditability, and strong privacy.
- Weaknesses: weaker semantic handling, substantial local dependency burden, and high risk of hand-tuned behavior with only 30 labeled examples.

#### Option B: one end-to-end schema-constrained multimodal model

One multimodal call receives message media and structured context and returns the complete decision.

- Strengths: simple semantic flow and potentially strong cross-modal understanding.
- Weaknesses: higher cost, latency, privacy exposure, provider dependence, reduced extraction observability, and less reusable caching.

#### Option C: cached multimodal extraction plus one text routing model

Media is decoded and semantically extracted in an isolated, cached stage. Deterministic context construction and retrieval then build a text routing packet for one schema-constrained routing model.

- Strengths: extraction observability, reusable caching, cost control, reproducibility, isolated failure domains, and one consistent final router across modalities.
- Weaknesses: extraction can lose nuance; extraction and routing errors compound; confidence must incorporate both stages; more versioned artifacts are required.

### Decision

The project owner selected **Option C** because it provides better observability, caching, cost control, and separation between extraction and routing.

The selection fixes the architectural boundary, not the provider or implementation library.

### Consequences

- Media extraction and routing MUST have separate contracts, error states, caches, telemetry, and test suites.
- The final router MUST be one text-based, schema-constrained model invocation per message attempt.
- The router MUST receive extracted media facts and extraction quality; it MUST NOT receive opaque success/failure blanks.
- Deterministic code MUST build the evidence allowlist before routing and validate the selected evidence afterward.
- Raw model responses and unpatched baseline predictions MUST remain immutable.
- Runtime-required specification artifacts MUST later be copied under `code/`.

## ADR-002: Deterministic boundaries

The following are locked as deterministic responsibilities:

1. input allowlisting and schema validation;
2. primary/composite key and join validation;
3. timestamp parsing and prior-only temporal filtering;
4. byte-signature media format detection;
5. cache identity and version invalidation;
6. retrieval candidate generation and ranking inputs;
7. evidence allowlisting, same-user validation, and provenance;
8. safety invariants, positive triggers, and negative controls;
9. confidence adjustment, penalties, caps, and rounding;
10. output row count, ID equality, enum, reason, confidence, and evidence validation.

## ADR-003: No patches

All logic MUST be generalized. Production message IDs and expected labels MUST NOT appear in prompts, decision tables, source branches, regressions, or cache keys. A failed case may change the system only through a documented general invariant with both positive triggers and negative controls.

## Open decisions requiring owner approval before implementation

These are deliberately not resolved by this specification:

1. extraction provider/model and whether OCR and ASR are local or remote;
2. routing provider/model and pinned model version;
3. API budget, concurrency, timeout, and retry ceilings;
4. whether message/media content may leave the machine;
5. cache backend, encryption, retention, deletion, and access controls;
6. embedding provider and similarity implementation for retrieval;
7. operational top-K candidate budget within the contract maximum;
8. supported languages and minimum OCR/ASR quality thresholds;
9. aggregate-feature snapshot assumption where no `as_of` timestamp exists;
10. production degraded-mode policy approval, especially generic `digest/unknown` fallback.

All numeric choices in the frozen specification that are not challenge-contract requirements are `PROVISIONAL-V0` configurations subject to baseline evaluation. This includes retrieval/confidence weights, thresholds, caps, evidence limits, top-K, time windows, retry limits, and internal rubric weights.

## Consistency-check findings

### Resolved specification ambiguities

- Text-only messages now use `media_state=not_applicable` with no extraction record; media messages require an extraction record.
- Missing media has no reusable content-hash cache entry and is checked again on later runs.
- The model may report contradiction candidates, but deterministic S8 code recomputes the contradiction count used for confidence.
- Safety rules may consume model semantic flags only as quoted semantic observations. Deterministic code owns source-span checks, signal combination, constraints, and confidence consequences.
- Raw model, contract-final, and degraded evaluation layers are reported separately, preventing deterministic validation from hiding model failures.

### Remaining tensions and implementation risks

1. Deterministic safety constraints deliberately narrow the model's action space. The model still proposes the final semantic decision; generic degraded output is the only non-model exception and remains owner approval pending.
2. Dataset timestamps have no timezone offset. Architecture v0.1 treats them consistently as dataset-local naive wall-clock values; quiet hours, recency, and near-term deadlines use the same convention without geographic inference or conversion.
3. Aggregate user/business/group features lack explicit snapshot timestamps. Their use may leak future behavior relative to individual target rows unless an assumption or ablation is approved.
4. Semantic safety flags are model-derived. Span verification prevents unsupported flags but cannot guarantee semantic correctness.
5. Extraction quality thresholds, language coverage, provider behavior, and decoder availability remain implementation choices.
6. Cache encryption, retention, access control, and deletion are unresolved privacy requirements.
7. Confidence weights and caps are a deterministic initial policy, not empirically validated calibration; the 30 labeled examples are too small for strong calibration claims.
8. Exact extraction/router models, budgets, retries, and top-K determine cost and reliability but remain unselected.
9. The internal evaluation rubric is not the undisclosed HackerRank scoring formula.
10. No minimum accuracy acceptance threshold is currently approved.

Until resolved, implementations MUST expose these as configuration and MUST NOT encode provider-specific assumptions as architectural facts.

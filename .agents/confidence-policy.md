# Confidence Policy

> **Configuration status:** The challenge `CONTRACT` requires confidence in `[0,1]`. Every other numeric weight, threshold, base value, penalty, cap, rounding rule, bin/support cutoff, and time window in this document is `PROVISIONAL-V0` and subject to baseline evaluation.

## Objective

Final confidence is a deterministic assessment of decision reliability, not the routing model's self-confidence. It MUST account for extraction quality, evidence strength, missing context, contradictory signals, and routing uncertainty.

## Inputs

All inputs are clamped to the challenge `CONTRACT` domain `[0,1]` and recorded per decision.

### 1. Routing certainty `R`

```text
R = 1 - routing_uncertainty
```

`routing_uncertainty` comes from the schema-constrained model response. Because it is model-reported, `R` receives only 30% weight and can be capped by deterministic factors.

### 2. Extraction quality `X`

For text-only messages, `PROVISIONAL-V0 X=1.00`.

For media messages:

| Media state | `PROVISIONAL-V0` base `X` |
|---|---:|
| `ok` | deterministic extractor quality score, minimum 0.70 |
| `low_quality` | extractor quality score, capped at 0.55 |
| `empty_extraction` | 0.25 when usable accompanying text exists; otherwise 0.05 |
| `unsupported` | 0.20 when usable accompanying text exists; otherwise 0.05 |
| `decode_failed` | 0.15 when usable accompanying text exists; otherwise 0.05 |
| `missing` | 0.10 when usable accompanying text exists; otherwise 0.05 |

### 3. Evidence strength `E`

For selected evidence, calculate the `PROVISIONAL-V0` formula:

```text
candidate_strength =
  0.35 * relationship_score +
  0.25 * recency_score +
  0.20 * semantic_score +
  0.20 * behavioral_score

E = 0.70 * max(candidate_strength) + 0.30 * mean(candidate_strength)
```

If no evidence is selected:

- `E=0.50` for content-intrinsic decisions that do not claim personalization, such as a clear greeting, forward, or safety classification;
- `E=0.20` for decisions whose reason claims personalized historical behavior, relationship, repetition, preference, or trust;
- `E=0.35` otherwise.

Evidence applicability is derived deterministically from the final type and reason-claim validator, not chosen freely by the model.

### 4. Context completeness `C`

All numeric component weights and state values below are `PROVISIONAL-V0`.

```text
C = 0.25 * user_context
  + 0.25 * conversation_context
  + 0.25 * relationship_context
  + 0.25 * prior_behavior_context
```

Each component is `PROVISIONAL-V0 1` when required fields are present and valid, `PROVISIONAL-V0 0.5` when partially available, and `PROVISIONAL-V0 0` when absent/invalid. For personal conversations, valid sender context satisfies relationship context. Optional absence remains unknown rather than negative.

### 5. Signal agreement `A`

Start at `PROVISIONAL-V0 1.00`, then subtract using the `PROVISIONAL-V0` contradiction table:

| Independent contradiction count | `A` |
|---:|---:|
| 0 | 1.00 |
| 1 | 0.70 |
| 2 | 0.40 |
| 3 or more | 0.10 |

S8 computes this count deterministically from validated semantic flags and structured facts. The model-reported contradiction count is diagnostic only and MUST NOT feed the formula.

## `PROVISIONAL-V0` formula

```text
support = 0.30*R + 0.20*X + 0.20*E + 0.20*C + 0.10*A
base_confidence = 0.05 + 0.93*support
final_confidence = clamp(base_confidence - P, 0.05, 0.98)
```

`P` is the sum of `PROVISIONAL-V0` explicit penalties, with `PROVISIONAL-V0 penalty_cap=0.35`:

| Penalty | Value |
|---|---:|
| critical relationship context missing for a relationship-dependent claim | 0.10 |
| aggregate snapshot time is undefended and materially used | 0.08 |
| deterministic fallback retrieval used after embedding failure | 0.04 |
| one semantic flag contradicts quoted content/structured facts | 0.08 |
| two or more such contradictions | 0.18 instead of 0.08 |
| router required a schema/constraint retry | 0.06 |

## Caps

Apply the lowest applicable `PROVISIONAL-V0` cap after the formula:

| Condition | Maximum confidence |
|---|---:|
| `low_quality` media materially used | 0.72 |
| `empty_extraction` with accompanying text | 0.60 |
| `missing`, `unsupported`, or `decode_failed` with accompanying text | 0.55 |
| unusable media and no message text | 0.40 |
| contradictory hard invariants | 0.35 |
| generic degraded routing | 0.10 |
| deterministic safety fallback after router exhaustion | 0.60 |

Round only once, after penalties and caps, using `PROVISIONAL-V0 decimal_places=4` and round-half-even.

## Monotonicity invariants

Holding all other inputs constant:

- greater routing uncertainty MUST NOT raise confidence;
- lower extraction quality MUST NOT raise confidence;
- weaker evidence MUST NOT raise confidence;
- more missing context MUST NOT raise confidence;
- more contradictions MUST NOT raise confidence;
- a retry MUST NOT raise confidence.

## Calibration and versioning

- The first complete evaluation run is the immutable raw baseline and uses this formula without fitted changes.
- Any later calibration mapping MUST be versioned, trained without reading target labels, and reported both before and after calibration.
- With only 30 samples, use leave-one-out or nested resampling for exploratory calibration and label results as high variance. Do not report in-sample calibration as expected generalization.
- Formula, component definitions, thresholds, penalties, caps, and rounding form the `confidence_policy_version` and participate in run identity.
- Calibration MUST be global or class-level with declared support; it cannot reference message IDs.

## Audit output

For every decision, retain out of band:

```text
R, X, E, C, A, support, each penalty, applied cap,
unrounded confidence, rounded confidence, policy version
```

These fields never enter the required submission CSV.

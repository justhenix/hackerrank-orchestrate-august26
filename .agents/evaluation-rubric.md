# Evaluation Rubric

> **Configuration status:** Unless marked `CONTRACT` or `DATASET FACT`, every numeric split, weight, threshold, cap, bin count, support cutoff, and time window in this document is `PROVISIONAL-V0` and subject to baseline evaluation.

## Deterministic development and sealed-holdout protocol

### Split definition

- `DATASET FACT sample_rows=30`.
- `PROVISIONAL-V0 development_rows=20` and `PROVISIONAL-V0 sealed_holdout_rows=10`.
- Stratify by the labeled `action` column using fixed `PROVISIONAL-V0 holdout_counts={notify:3,digest:4,mute:3}`. The corresponding `PROVISIONAL-V0 development_counts={notify:6,digest:7,mute:7}`.
- A trusted split step computes `sha256("architecture-draft-v0.1-split|" + message_id)` for each row, sorts ascending within each action, and assigns the first configured holdout count to the sealed holdout. This use of IDs is only deterministic evaluation partitioning, never routing logic.
- The split manifest records the algorithm/version, source-file hash, salt identifier, partition counts, and manifest hash. It is immutable for Architecture v0.1.

### Sealing rules

- The development partition MAY influence prompts, generalized rules, provisional thresholds/weights/caps, provider choice, and implementation quality.
- Holdout row contents, labels, per-row predictions, metrics, and errors MUST remain inaccessible before freeze.
- Only a holdout runner/evaluator may read the sealed rows. The router still receives only the sanitized 11 input columns.
- Aggregate holdout allocation counts above are permitted; no holdout row ID or label mapping may appear in runtime configuration or development reports.
- Development-run caches MUST NOT expose holdout extraction or routing results. Holdout execution uses the frozen configuration and a clean logical run namespace; content-addressed extractor cache reuse is allowed only if created without labels and its provenance is recorded.

### Freeze and final 30-row run

Configuration freeze records the code commit/tree hash; prompt, schema, invariant, confidence, retrieval, split, and error-policy versions; extractor/router model versions; all `PROVISIONAL-V0` values; dependency lock; and input hashes.

After freeze:

1. run the frozen pipeline over `DATASET FACT sample_rows=30` using `PROVISIONAL-V0 frozen_baseline_runs=1`;
2. report development 20, sealed holdout 10, and combined 30 metrics separately;
3. preserve raw model, contract-final, and degraded layers;
4. treat holdout results as reporting-only: they MUST NOT change this submission's architecture, prompts, rules, thresholds, weights, caps, retrieval, extractors, router, or model choice.

If a later research cycle nevertheless studies holdout results, those rows permanently cease to be an unseen holdout for that later work and cannot be resealed. Such work is outside this frozen submission baseline and MUST be disclosed.

## Immutable raw baseline

The raw baseline is the first full, contract-valid run over all `DATASET FACT sample_rows=30` sanitized sample rows after architecture/configuration freeze and before any holdout inspection.

The baseline bundle MUST contain:

- run manifest and all input/configuration hashes;
- extraction records and cache hit/miss metadata;
- routing packets or their protected canonical hashes;
- every raw model response and retry;
- unmodified parsed raw decisions;
- final deterministic confidence components;
- baseline `predictions.csv`;
- metric output and error inventory.

The bundle is append-only/immutable. Any correction creates a new run ID. No tool may overwrite, filter, relabel, drop, reorder, or patch baseline rows.

## Leakage controls

1. Evaluation code reads labels; router code never does.
2. The evaluator creates a sanitized input containing only the 11 input columns before invoking the pipeline.
3. Historical candidates must be same-user and strictly earlier than the evaluated message.
4. Sample/target messages never become history during the run.
5. Target outputs and hidden labels are unavailable to tuning.
6. Recursive file discovery is forbidden; use an allowlist.
7. Cache keys contain content/config/model/schema versions, never expected labels.
8. Prompt, threshold, rule, and regression changes require a new version and a new unpatched run.
9. Aggregate features without snapshot timestamps are isolated for ablation/sensitivity reporting.
10. Regression cases use synthetic generalized fixtures, never production message IDs or copied labels.

## `PROVISIONAL-V0` internal 100-point rubric

This rubric is for reproducible development comparison; it does not claim to match undisclosed HackerRank weights.

| Area | Weight | Measures |
|---|---:|---|
| action quality | `PROVISIONAL-V0 30` | accuracy, macro-F1, per-class recall/confusion |
| message-type quality | `PROVISIONAL-V0 20` | accuracy, macro-F1, per-type support/confusion |
| joint decision quality | `PROVISIONAL-V0 10` | exact action+type match |
| evidence quality | `PROVISIONAL-V0 15` | validity, same-user/prior provenance, precision, recall, coverage, relevance audit |
| confidence quality | `PROVISIONAL-V0 10` | Brier score, ECE, reliability bins, accuracy by confidence band |
| reason quality | `PROVISIONAL-V0 5` | nonempty, concise, personalized when supported, no unsupported claim |
| contract robustness | `PROVISIONAL-V0 5` | schema-valid rate, ID coverage, enum/range/serialization validity |
| operations | `PROVISIONAL-V0 5` | latency, cost, retry rate, cache hit rate, degraded/error rate |

## Required metric report

### Predictive

- action accuracy and macro-F1;
- action confusion matrix;
- message-type accuracy and macro-F1;
- type per-class support and recall;
- joint action/type exact match.

### Evidence

- valid-ID rate;
- same-user and prior-time rate;
- exact-set precision/recall/F1 against sample evidence;
- evidence coverage;
- semantic relevance review, because alternate valid evidence may exist.

### Confidence

- multiclass Brier score when action probabilities are available; otherwise correctness Brier score using final confidence;
- expected calibration error with declared bins;
- confidence distribution by correct/incorrect and by media state;
- raw uncertainty versus deterministic final confidence.

### Contract and operations

- missing/duplicate/extra output rows;
- invalid enum, reason, confidence, or evidence rows;
- extraction state distribution;
- error/degraded-mode counts;
- p50/p95 latency by stage;
- extraction and routing cache hit rates;
- model calls, tokens, and estimated cost;
- reproducibility check across an identical cached run.

## Evaluation layers

Report these separately:

1. **raw model layer:** parsed model proposal before deterministic confidence, invariant retry, or fallback;
2. **contract final layer:** generalized invariants, validated evidence, and deterministic confidence applied;
3. **degraded layer:** rows produced only after failure/retry exhaustion.

Never hide raw-layer failures behind final-layer metrics. Never call a manually patched run a baseline.

## Sample limitations

- Only 30 labeled rows exist.
- `payment` has zero examples; several types have one example.
- Confidence labels occupy a narrow high range.
- Official scoring weights and reason/evidence equivalence rules are unspecified.

Consequently:

- use only the deterministic 20-row development / 10-row sealed holdout protocol above;
- do not approve architecture changes from tiny aggregate differences alone;
- show exact counts and intervals where possible;
- treat per-type results with fewer than `PROVISIONAL-V0 support_threshold=5` examples as descriptive only;
- preserve operational and regression evidence alongside label metrics.

## Acceptance gates before target generation

- `CONTRACT 100%` output schema validity on samples and regressions;
- `CONTRACT 100%` evidence provenance validity;
- zero future/cross-user evidence;
- all media states and decoder mismatch regressions pass;
- confidence monotonicity tests pass;
- raw baseline bundle is immutable and reproducible;
- no production message ID or expected-label patch appears in runtime/configuration.

No minimum accuracy threshold is set here because the owner has not approved one.

# Architecture Specification

## Status

The project owner selected **Option C: cached multimodal extraction followed by one text-based, schema-constrained routing model** after reviewing the repository inspection.

The specification is frozen as **Architecture v0.1** as of 2026-08-01.

This freeze records the current architecture and stage contracts. It does not
freeze `PROVISIONAL-V0` runtime configuration; configuration freeze remains the
separate M5 activity defined in `orchestrator.md` and `evaluation-rubric.md`.

This directory formalizes and consistency-checks that selection. It does not implement the runtime, alter the dataset, or replace the project contracts in `AGENTS.md`, `problem_statement.md`, and `README.md`.

Normative terms use RFC-style meanings:

- **MUST / MUST NOT**: required for architectural conformance.
- **SHOULD / SHOULD NOT**: expected unless a documented decision explains otherwise.
- **MAY**: permitted but optional.

## Provisional v0 configuration label

`PROVISIONAL-V0` labels every numeric weight, threshold, cap, top-K value, evidence-count limit, retry limit, reason-length limit, and time window that is not imposed by the challenge contract. These values are initial configuration for the first baseline, not architectural truths. They MUST be versioned, reported in the run manifest, and evaluated through the development/holdout protocol before configuration freeze.

Numeric values imposed by the challenge contract, such as confidence being within `[0,1]`, are labeled `CONTRACT`. Dataset facts and document/stage numbering are descriptive, not tunable configuration.

## Design boundary

Deterministic code owns:

- schema and join validation;
- temporal filtering;
- media format sniffing;
- retrieval candidate generation;
- evidence allowlisting and provenance;
- extraction and routing cache identity;
- safety invariants and negative controls;
- confidence adjustment;
- final CSV validation;
- immutable raw evaluation artifacts.

Models own only:

- semantic extraction from decoded media;
- semantic interpretation of the routing packet;
- one final structured routing proposal.

Exact `@user_id` mention detection is deterministic. Model-derived indirect addressing is only a semantic signal and cannot, by itself, bypass quiet-hours or muted-group constraints.

The routing model MAY select evidence only from the historical IDs supplied in its routing packet. It cannot introduce, retrieve, or invent IDs.

## Specification map

| File | Purpose |
|---|---|
| `orchestrator.md` | End-to-end stages, inputs, outputs, ownership, failures, fallbacks, and tests |
| `decisions.md` | Architecture decision record, considered alternatives, selected option, and open decisions |
| `decision-contract.md` | Canonical data contracts for extraction, retrieval, model response, and final CSV rows |
| `safety-invariants.md` | Non-bypassable boundaries, positive triggers, negative controls, and degraded-mode behavior |
| `confidence-policy.md` | Deterministic confidence features, formula, penalties, caps, and calibration rules |
| `evaluation-rubric.md` | Immutable raw baseline, leakage controls, metrics, and reporting rules |
| `error-taxonomy.md` | Stable error codes, severity, retryability, and exact fallback classes |
| `regression-cases.yaml` | Generalized, message-ID-independent regression cases |

## Source-of-truth order

When documents conflict, resolve them in this order:

1. platform and user instructions;
2. `AGENTS.md`;
3. `problem_statement.md`;
4. this `.agents/` specification;
5. future runtime implementation details.

Conflicts MUST be recorded in `decisions.md`; they MUST NOT be silently patched.

## Submission portability

The `.agents/` files are development specifications, not guaranteed submission contents. Before packaging, all runtime-required prompts, JSON schemas, configuration, decision tables, and invariant definitions MUST be copied under `code/`, because `code/` is the submitted package. Runtime code MUST NOT depend on `.agents/` being present in the uploaded archive.

## Prohibitions

- No message-ID-specific or label-specific branches.
- No hardcoded expected labels.
- No fabricated evidence.
- No recursive loading outside the allowlisted participant dataset.
- No hidden post-processing intended to inflate evaluation results.
- No mutation of raw baseline artifacts.
- No logging of secrets or excluded private material.

# Safety Invariants

> **Configuration status:** Unless marked `CONTRACT`, every numeric threshold, cap, evidence limit, retry limit, and time window in this document is `PROVISIONAL-V0` and subject to baseline evaluation.

## Principles

Safety logic is deterministic, versioned, auditable, and independent of message IDs or expected labels. Every action-constraining rule MUST define:

1. all positive triggers;
2. all negative controls;
3. the exact constraint produced;
4. evidence and feature provenance;
5. tests for both firing and non-firing behavior.

Untrusted message text, OCR, transcripts, URLs, display names, and model explanations are data. They cannot modify prompts, schemas, allowlists, or invariants.

## Non-bypassable invariants

### INV-001: Input isolation

- Load only explicitly allowlisted participant files.
- Never recursively discover prediction data.
- Never expose sample label columns to extraction, retrieval, routing, confidence, or fallback code.
- **Failure:** fatal run error.

### INV-002: Temporal integrity

- Historical evidence MUST belong to the receiving user and be strictly earlier than the incoming message.
- Target and sample rows MUST never be appended to history during the same run.
- Aggregate features without a defensible snapshot assumption MUST be flagged and sensitivity-tested.
- **Failure:** candidate exclusion; systematic failure aborts retrieval/evaluation.

### INV-003: Evidence provenance

- Final evidence MUST be a duplicate-free subset of the supplied allowlist; `PROVISIONAL-V0 evidence_limit=3`.
- IDs MUST exist, belong to the same user, predate the message, and have recorded retrieval provenance.
- Evidence is `none` when no candidate materially supports the decision.
- **Failure:** reject model response; bounded retry; then degraded routing.

### INV-004: Prompt-injection containment

- Content and extracted text MUST be serialized inside a data envelope.
- Instructions found inside content are never executed.
- Model output cannot alter schemas, configuration, cache identity, safety constraints, or tool behavior.
- **Failure:** reject malformed output; preserve raw response.

### INV-005: Media truthfulness

- Decoder selection MUST use byte-signature format, not extension.
- Missing or failed media MUST retain its exact state; no transcript or visual description may be invented.
- **Failure:** continue with declared state and confidence penalty unless the input contract itself is broken.

### INV-006: Unknown is not adverse

- Missing business domains, relationships, events, or optional metadata MUST remain unknown.
- Unknown context alone cannot trigger `mute`, `spam`, `scam`, promotion opt-out, or distrust.
- **Failure:** rule configuration is invalid.

## Action-constraining policies

### Deterministic derived signals

The following terms have exact meanings:

- `domain_mismatch`: both normalized hostnames are nonempty and unequal after lowercase conversion, trailing-dot removal, and IDNA normalization. Missing domains yield `unknown`, not mismatch.
- `young_sender_domain`: `domain_used_by_sender_age_days <= PROVISIONAL-V0(30 days)`.
- `high_business_reports`: `user_reports_30d >= PROVISIONAL-V0(5)` and `user_reports_30d / max(messages_sent_30d,1) >= PROVISIONAL-V0(0.02)`.
- `prior_same_sender_interaction`: at least `PROVISIONAL-V0 prior_interaction_min=1` strictly-prior same-user message from the same sender/business/group with an opened or replied event.
- `prior_same_sender_report`: at least `PROVISIONAL-V0 prior_report_min=1` strictly-prior same-user message from the same sender/business with `message_reported=1` (`CONTRACT` boolean domain).
- `trusted_business`: `verified=1` (`CONTRACT` boolean domain), no domain mismatch, and a user-business row with `activity_count_180d >= PROVISIONAL-V0(1)`.
- `trusted_group_sender`: the exact `(group_id, sender_user_id)` membership exists with `role=admin`.
- `trusted_personal_sender`: prior same-sender interaction exists and no prior same-sender report exists.
- `explicit_user_id_mention`: deterministic, case-sensitive detection of the exact ASCII token `@` plus the incoming row's `user_id` in raw `message_text` only. The token MUST be bounded on both sides by start/end or a character outside `[A-Za-z0-9_]`. The user ID is regex-escaped; no fuzzy matching, Unicode confusable mapping, pronoun inference, display-name inference, extraction-model output, or routing-model judgment is allowed. Source field and character offsets are recorded. Mentions found only in extracted media are semantic candidates and cannot bypass quiet-hours or muted-group constraints by themselves.
- `near_term`: the model reports `time_critical=true`, supplies a span-verified `deadline_at`, and that deadline is after but no more than `PROVISIONAL-V0(2 hours)` after incoming `created_at`; if no valid deadline exists, this signal is false.
- `usable_text`: Unicode-trimmed message text contains `PROVISIONAL-V0 alphanumeric_min=1`.

All timestamps use the dataset-local naive wall-clock convention. Code MUST NOT infer a geographic timezone, attach an offset, or convert source values. Quiet hours, recency, deadlines, and prior-only comparisons use that same convention. Model flags cannot create metadata facts; deterministic code recomputes every derived signal.

### INV-101: Corroborated high-risk request

**Positive trigger:** either path A or path B must hold.

- Path A requires `PROVISIONAL-V0 identity_signal_min=1` and `PROVISIONAL-V0 request_signal_min=1`.
  - Identity risk: `(verified=0 and domain_mismatch)`, `(domain_mismatch and young_sender_domain)`, `high_business_reports`, or `prior_same_sender_report`.
  - Request risk: `credential_or_secret_request=true`, or both `suspicious_link_or_payment_request=true` and `impersonation_or_domain_concern=true`.
- Path B applies to a sender with no prior same-sender interaction and requires `PROVISIONAL-V0 request_signal_min=2`: both `credential_or_secret_request=true` and `suspicious_link_or_payment_request=true`.

Model semantic flags MUST cite bounded source spans in protected diagnostics, and deterministic code verifies that the cited spans occur in message/extraction data. A single keyword, forwarding count, missing field, or model flag without a source span is insufficient.

**Negative controls:**

- `trusted_business=true`, `transactional=true`, and no credential/secret request;
- `trusted_group_sender=true` or `trusted_personal_sender=true`, `transactional=true`, and neither request-risk condition holds;
- `warning_or_quoted_discussion=true` with a valid support span, while `indirectly_addresses_user=false` and `explicit_user_id_mention=false`.

**Constraint:** when a positive path holds and no negative control holds, `required_action=mute`; allowed types are `scam`, `spam`, or `unknown` according to the model's supported semantics.

### INV-102: Explicit promotion opt-out

**Positive trigger:** all conditions are required:

- relationship row exists;
- opt-out timestamp exists and predates the incoming message;
- model semantic flag `promotional=true`;
- model semantic flag `transactional=false`.

**Negative controls:** order status, booking update, payment receipt/reminder, account security, service outage, or safety notice; absent relationship row; opt-out timestamp after the message.

**Constraint:** `required_action=mute`, type must be `promotion` or `spam` as semantically supported.

### INV-103: Quiet hours

**Positive trigger:** message timestamp falls inside the configured receiving user's do-not-disturb window.

**Negative controls for prohibiting notify:** `near_term=true` plus one of `trusted_business`, `trusted_group_sender`, `trusted_personal_sender`, or `explicit_user_id_mention=true`, with no high-risk invariant. `indirectly_addresses_user=true` alone never qualifies.

**Constraint:** absent the negative-control exception, add `notify` to prohibited actions. Quiet hours do not force `mute` and do not change message type.

### INV-104: Muted group

**Positive trigger:** `group_muted_by_user=1` for the exact user/group pair.

**Negative controls for prohibiting notify:** (`time_critical=true` and `explicit_user_id_mention=true`) or (`trusted_group_sender=true` and `near_term=true`), with no high-risk invariant. Model-derived indirect addressing alone never qualifies.

**Constraint:** absent an exception, prohibit `notify`. A muted group does not automatically force `mute` or imply low value.

### INV-105: Contradictory hard constraints

If independently derived invariants simultaneously require incompatible actions, the system MUST NOT use evaluation labels, rule order, or arbitrary priority to choose.

**Constraint:** prohibit `notify`, emit `INVARIANT_CONTRADICTION`, use `PROVISIONAL-V0 constraint_retry_limit=1`, then use degraded routing. A high-risk `mute` requirement remains dominant only when its complete corroborated trigger still holds.

## Confidence safety constraints

- Any non-`ok` media state MUST affect final confidence according to `confidence-policy.md`.
- Out-of-allowlist evidence cannot be converted to `none` silently; it invalidates the response.
- Missing context and contradictory signals MUST lower confidence monotonically.
- Degraded decisions MUST use declared caps and error records.

## Privacy and logging invariants

- Never log secrets, credentials, raw authentication material, or excluded private content.
- Raw message/media/model artifacts MUST follow the owner-approved storage and retention policy before implementation.
- Logs SHOULD use IDs, hashes, stable reason codes, and bounded excerpts; full content requires explicit necessity and protection.
- External model use MUST be disabled until the owner approves the data-egress boundary.

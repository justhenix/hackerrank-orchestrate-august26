# Architecture and tradeoff notes

## Architecture

- The input boundary projects each message to exactly eleven label-free
  fields. Development and holdout labels remain evaluator-side.
- Typed CSV loading performs exact headers, enums, numeric bounds, keys, and
  foreign-key checks before normalized nullable joins are built.
- Retrieval is same-user and strictly earlier, with deterministic relationship,
  recency, and behavioral features. Candidate order is stable and the packet
  carries a validated evidence allowlist.
- The provider interface separates multimodal extraction from the single
  structured routing call. The Gemini adapter selects Vertex ADC explicitly;
  fake providers keep tests offline.
- Raw responses are written before parsing. Each run has a write-once manifest,
  packet hashes, per-attempt accounting, final decisions, and an error
  taxonomy.
- Successful media extraction is content/config/model addressed. A target
  output is written atomically only after all final decisions pass the exact
  CSV and provenance contract.

## Tradeoffs

- Strict contracts fail closed. A malformed or unsafe model response becomes a
  deterministic degraded fallback rather than silently repaired output.
- The system uses deterministic non-embedding retrieval for reproducibility;
  semantic relevance can improve later without changing the packet contract.
- Confidence is the frozen deterministic policy, not a label-fitted calibrator.
  The reported Brier and ECE are evaluation observations, not tuning inputs.
- Vertex calls are serialized and bounded. This favors spend control and
  reproducibility over throughput.
- The local price table was left at zero for this run, so accounting is
  explicitly reported as configured cost rather than asserted billing truth.

## Interview talking points

1. The most important safety property is label isolation: the router cannot
   read expected outcomes, even though the evaluator can score the run later.
2. Raw-attempt preservation makes provider, schema, and safety failures
   diagnosable without weakening validation or rewriting model output.
3. Evidence is provenance-constrained before it reaches the final output;
   model-selected IDs cannot introduce arbitrary history or target IDs.
4. The first baseline failure was infrastructure, not prediction quality: the
   configured publisher model returned Vertex 404 before generation. The fix
   was explicit model/configuration selection and generalized diagnostics.
5. The final target artifact is reproducible from the frozen code, environment
   switch, run ID, and ignored raw artifact directory; the output itself is
   validated independently of the model provider.

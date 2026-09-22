# Retained consumer compatibility review

Reviewed 2026-09-22 against the source snapshots retained from the 2026-09-19 ecosystem scan. This is a list of downstream expectations, **not the authoritative provider contract**. No clients or paid inference calls were executed during this review.

## Consumer expectations

| Consumer | Verified source behavior | Consequence for Jevmulator |
|---|---|---|
| fast-jev-compaction | `buildJevRequest` emits bearer auth and `{model,state,questions}`. Its `baseUrl` is a complete endpoint URL, not a prefix. `parseJevResponse` requires only an `answers` object. `noulAnswer` requires a finite number but does not range-check it. | Suitable lightweight HTTP consumer for conformance testing. Point its `baseUrl` at the full local `/v1/systemone` URL. Its permissive parser cannot certify full schema parity. |
| jev-ultrafast | Uses the literal provider endpoint. Sends structured objects in instructions and choice descriptions. `validate_choice` requires exact probability-key coverage, finite numbers in [0,1], sum error strictly below 0.02, and an argmax-consistent selected choice. Reads `result["model"]`. | A configurable endpoint seam is needed to redirect this snapshot. String-only question schemas would reject real client requests. Reuse the validator as an additional consumer acceptance check. |
| jev-review | Uses a literal endpoint, but accepts an injected fetch function. Its Zod schema requires `model`, `answers`, and nonnegative integer usage counts; answer types are discriminated. Score legends accept only string values. Retries 429, 529, and other 5xx statuses. Checks `detail.error_type` for `max_tokens_exceeded`. | Test through its transport seam. The client narrows some official SDK types: structured legends and nullable token counts require separate compatibility cases, not changes to the provider contract. |
| Foreman | Uses the official asynchronous SDK and accepts an injected client. Parses `.nouls` first; a mapping fallback also exists. Requires all ten assessment names. Rejects booleans/nonfinite values, but clamps numeric values into [0,1]. | A real SDK round trip is required to exercise wire parsing; passing a mapping or fake object proves only local policy compatibility. Clamping is a consumer behavior, not evidence that the provider permits out-of-range probabilities. |
| NanoJev demo server | Serves `/api/evaluate`; expects a `states` collection and emits its own execution metadata. Local limits include 32 states, 96 questions and 256 candidate paths. | Reference implementation only. These routes, envelopes, and limits must not be copied into the Jev compatibility contract. |

## Local evidence

Paths below are retained snapshots; line numbers refer to those files.

- [fast-jev-compaction request construction and parsing](../../knowledge-extractions/intermediate/2026-09-19-jev-repo-scan/repos/fast-jev-compaction/src/request.ts): lines 11-80.
- [jev-ultrafast model client](../../knowledge-extractions/intermediate/2026-09-19-jev-repo-scan/repos/jev-ultrafast/jev_ultrafast/model.py): validator lines 30-45; structured requests and literal endpoint lines 81-119.
- [jev-review schema](../../knowledge-extractions/intermediate/2026-09-19-jev-repo-scan/repos/jev-review/src/jev/schema.ts): lines 1-48.
- [jev-review client](../../knowledge-extractions/intermediate/2026-09-19-jev-repo-scan/repos/jev-review/src/jev/client.ts): transport injection, error extraction and retries.
- [Foreman adapter](../../knowledge-extractions/intermediate/2026-09-19-jev-repo-scan/repos/foreman/src/foreman/foreman/jev.py): `normalize_assessment`, `parse_jev_response`, `JevForemanModel`.
- [NanoJev server](../../knowledge-extractions/intermediate/2026-09-19-jev-repo-scan/repos/NanoJev/scripts/serve_decisions.py): lines 31-52.

Official SDK response types used for comparison: <https://docs.typesafe.ai/sdk/python/api/types/responses>. The completed [provider contract](CONTRACT-2026-09-22.md) pins the live OpenAPI as the wire authority and preserves SDK differences separately. In particular, nullable usage is SDK tolerance rather than the provider's published success shape. A live documentation link is not itself a version pin.

## Acceptance criteria for the later implementation

1. Validate request and response fixtures against the pinned provider wire schema. Preserve the provenance label on every fixture: official example, public observed response, or locally synthesized test case.
2. Exercise the official Python and JavaScript SDKs against the local HTTP daemon, covering all three primitives, mixed question batches, structured instructions/criteria, model metadata, usage, and documented errors.
3. Exercise retained raw HTTP consumers through endpoint or transport overrides. Do not mistake a hardcoded provider hostname for an emulator schema failure.
4. Check semantic consistency separately from JSON shape: choice equals an argmax, probability keys cover the candidates, score agrees with its documented distribution calculation, and question IDs map back correctly.
5. Keep malformed inputs, malformed upstream model output, and upstream transport failure distinct. A model failure must not become a successful invented distribution.
6. Test question-ID renaming and adding unrelated questions as separate behavioral properties. SDK parsing cannot establish evaluation independence or model calibration.
7. Report schema conformance, consumer compatibility, semantic fidelity, and performance separately. Passing the first two does not demonstrate equivalent Jev judgments.

## Status

Static source review complete. No runtime compatibility claim is made here. No emulator implementation was created.

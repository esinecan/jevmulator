# Compatibility boundaries

This file records what Jevmulator guarantees, what it decides for itself, and what remains
unknown about TypeSafe. The README summarises it. This is the long form, with the evidence
for each claim.

The rule behind every entry: a published schema is evidence about the schema. It is not
evidence about runtime behaviour. A published algorithm in a client library is evidence
about that library. It is not evidence about the service.

---

## 1. What Jevmulator guarantees

These hold for every successful response, and the test suite checks each one.

1. The response satisfies the pinned `SystemOneResponse` schema, including required integer
   usage counts and a nonempty `answers` object.
2. `answers` has exactly one entry per submitted question, keyed by the exact submitted
   question ID.
3. Each answer's `type` matches its question's `type`.
4. A choice answer's `probabilities` keys are exactly the submitted criteria labels.
5. A choice answer's `choice` is a highest-probability label of its own distribution.
6. A score answer's `legend` repeats each submitted level description, structure preserved,
   keyed `"0"` to `"N-1"`.
7. A score answer's `probabilities` uses the same keys as its `legend`.
8. A score answer's `score` equals the expected value of its own distribution.
9. Every probability, confidence value and noul value lies in `[0, 1]`, and no value is NaN
   or infinite.
10. A question that cannot be answered fails the whole request. There is no partial
    `answers` object and no invented distribution.
11. No question ID, no sibling question and no sibling answer reaches the upstream model.

Guarantees 1 to 10 hold on both `/v1/systemone` and `/sys1/v1/systemone`. Guarantee 11 holds
only on `/v1/systemone`. On `/sys1`, one agent answers every question of a request in one
context, so sibling questions and the agent's own earlier answers are in its view. What
still holds there: no caller question ID reaches the agent, which sees the opaque labels
`q1..qN`, and the map back to the IDs is written to disk only after the agent's processes
have ended. Section 8 records the rest of what sys1 changes.

---

## 2. Decisions Jevmulator makes, which TypeSafe has not published

Each of these is a policy. None is a parity claim.

### Confidence

Computed with the two algorithms published in
`typesafe-ai/system-one-adapter-python` at commit
`e1d4cc938204b22fc5a3c3aca7044072fe3f712d`, file
`src/system_one_adapter/_utils/confidence_metrics.py`:

- choice: `(max(p) - 1/N) / (1 - 1/N)`, and `1.0` when `N == 1`
- score: first modal index `m`, `D = sum(p_i * |i - m|)`, `U = mean(|i - (N-1)/2|)`,
  result `max(0, 1 - D/U)`, and `1.0` when `N == 1`

These are verified adapter algorithms. They are not verified TypeSafe production algorithms.
TypeSafe's confidence documentation presents the choice formula and calls its own worked
example an approximation. Some published example confidence numbers do not equal these
formulas.

### Choice ties

The earliest label in the request's `criteria` order wins. TypeSafe does not document its
tie rule.

### Rounding

None is applied. Full floating-point precision is emitted. TypeSafe's precision and rounding
mode are not published, and rounding before an argmax could change the selected choice.

### Normalisation

A distribution whose sum differs from one by more than `1e-6` is rescaled, and the rescaled
values are what the response carries. The tolerance is the value the official adapter
publishes. Turn it off with `JEVMULATOR_NORMALIZE_PROBABILITIES=0`.

A distribution whose values are all zero is **rejected**, not replaced by a uniform
distribution. The official adapter substitutes uniform. Jevmulator treats a zero total from
a language model as a failed answer rather than a neutral one, because a uniform
substitution would turn a failure into a confident-looking judgment with confidence 0.

### Error bodies

The 422 body follows the published `HTTPValidationError` schema. Every other status carries
`{"detail": {"error_type": ..., "message": ...}}`. That object form is what the retained
`jev-review` consumer reads, and it is one of the envelopes both official SDK error parsers
accept. TypeSafe's exact non-422 bodies are not published.

### Status mapping

| Upstream condition | Jevmulator status |
|---|---|
| 429 after retries | 429, `retry-after` forwarded when supplied |
| 503 or 529 after retries | 529 |
| Other 5xx or 4xx after retries | 502 |
| Deadline exceeded | 504 |
| Unusable answer after repair retries | 502 |
| Refusal | 502 |

### Model names

`GET /v1/models` lists `jev-latest`, `jev-preview`, `jevmulator-latest` and
`jevmulator-<version>-<upstream model>`. The two `jev-` names exist so an unmodified client
works; both official SDKs insert `jev-latest` when the caller names no model. Every
description says the answer comes from the configured upstream model and not from TypeSafe
weights. Every successful response reports the resolved versioned identity.

`release_date` is `2026-09-22`, the contract pin date. It is not a TypeSafe release date.

### Token counts

`usage` carries the sum of the counts the upstream provider actually returned, across one
call per question. When a provider returns none, the header `X-Jevmulator-Usage-Source`
reads `upstream-partial`, and `JEVMULATOR_USAGE_POLICY=strict` fails the request instead.
No count is invented.

These are upstream tokens. They are not TypeSafe tokens, and a Jevmulator request makes
several upstream calls where a Jev request makes one.

### Size guards

`JEVMULATOR_MAX_STATE_CHARS` and `JEVMULATOR_MAX_REQUEST_CHARS` count characters. The
documented Jev limits are 64k tokens for state plus all questions and 32k tokens for state
plus the longest question, under a tokenizer that is not published. The guards are off by
default and are an approximation when enabled.

---

## 3. Where Jevmulator differs from the official adapter

`typesafe-ai/system-one-adapter-python` is a client library backed by an LLM, not an HTTP
daemon. It is useful reading. It is not a reference for Jev's semantics.

| Topic | Official adapter | Jevmulator |
|---|---|---|
| Calls per request | One call for all questions. `_client.py:_prepare` builds one system and one user message. | One call per question. |
| Question IDs | Model input. `_schema.py:create_llm_output_model` aliases each answer field to the caller's question ID. | Never model input. The upstream answer key is the fixed literal `answer`. |
| Question independence | Not enforced. All questions share one prompt and one completion. | Enforced and tested against recorded upstream payloads. |
| Zero-total distribution | Replaced by a uniform distribution. | Rejected. |
| Minimum criteria | Rejects a choice or score with fewer than two criteria. | Accepts one, because the OpenAPI sets `minItems: 1` for score and no minimum for choice. |
| Extra response fields | Adds debug information and its own usage fields. | Emits the pinned envelope only. |
| Transport | A Python client object. | An HTTP daemon with authentication, `/v1/models` and status mapping. |

No adapter file is vendored. The captured snapshot contains no LICENSE file, so its terms
are unknown to this project. The two formulas above are re-implemented with attribution;
see `NOTICES.md`.

---

## 4. Where the layers of the contract disagree

The pinned OpenAPI wins in every row. The other layer is preserved as a separate fact, not
enforced.

| Disagreement | Jevmulator's behaviour |
|---|---|
| The HTTP prose page marks `instructions` required. The OpenAPI and both SDKs make it optional and nullable. | Accepts omission and `null`. |
| The prose narrows a score legend to a string. The OpenAPI and SDKs allow objects and arrays. | Accepts and returns structure. |
| The JavaScript SDK's types permit a null `state` and null score descriptions. The OpenAPI excludes both. | Rejects both, with 422. |
| The Python SDK's public response tolerates missing or null usage. The wire schema requires integers. | Always sends integers. |
| The score docs recommend at least two levels. The schema sets `minItems: 1`. | Accepts one level. Its `score` is 0.0 and its `confidence` is 1.0. |
| Numeric ranges and a 255-option limit appear in prose but not in the schema. | Not enforced as schema. Range checks run as separate semantic checks on the output. |
| SDK versions before 0.6 used an integer-keyed score criteria dictionary. | Not supported. The pin uses the ordered array. |

No request schema in the pinned snapshot sets `additionalProperties: false`, so unspecified
properties are accepted at the top level and inside a question. Accepting them is not a
claim that TypeSafe preserves or uses them.

---

## 5. Retained consumer narrowings

These clients are stricter than the pinned contract. The narrowing belongs to the client.

| Consumer | Narrowing | Consequence |
|---|---|---|
| `jev-review` | Its Zod schema accepts only **string** score legend values. | Send string levels to that client, or widen its schema. Structured levels are valid on the wire. |
| `jev-review` | Requires nonnegative integer usage counts. | Satisfied. Jevmulator always emits integers. |
| `jev-review` | Reads `detail.error_type` looking for `max_tokens_exceeded`. | Satisfied. Non-422 errors carry an object `detail` with `error_type`, and the character guard emits that exact code. |
| `jev-ultrafast` | Requires exact probability-key coverage, finite values in `[0, 1]`, a sum error strictly below 0.02, and an argmax-consistent choice. | Satisfied. |
| `jev-ultrafast` | Uses the literal provider endpoint. | Needs an endpoint override to reach a local daemon. That is a client change, not a daemon defect. |
| `fast-jev-compaction` | Its `baseUrl` is a complete endpoint URL, not a prefix. | Point it at `http://127.0.0.1:8769/v1/systemone`. |
| `fast-jev-compaction` | Its parser requires only an `answers` object and does not range-check. | Satisfied, but that parser cannot certify parity. A test records this. |
| `Foreman` | Clamps numeric values into `[0, 1]`. | Never exercised. Jevmulator does not emit a value outside the range. Clamping is consumer behaviour, not provider permission. |
| `NanoJev` | Serves `/api/evaluate`, expects a `states` collection, and caps at 32 states, 96 questions and 256 candidate paths. | A reference implementation, not a target. That route returns 404 here, that body returns 422, and no such limit is imposed. |

The Python SDK converts score map keys to integers in Python. The HTTP JSON keys stay
strings. Both are correct; they describe different layers.

---

## 6. Still unknown about TypeSafe

Nothing in this repository asserts any of these as TypeSafe behaviour.

1. **Production numeric semantics.** The confidence formula, decimal precision, rounding
   mode, whether a score is computed before or after rounding, and the choice tie rule.
2. **Runtime validation edges.** Whether a one-level score, a zero-option choice, an empty
   string state or an empty object state is accepted at runtime, and what happens to
   unspecified properties. The schema permits all of them.
3. **Error parity.** The exact 401, 422, 429 and 529 bodies and headers, the text of
   validation messages, validation ordering, and the behaviour for an unknown model name.
4. **Token accounting.** The tokenizer, the billing rules, and the exact token boundaries
   behind the documented 64k and 32k limits.
5. **Calibration.** Whether any GLM Flash probability resembles the Jev probability for the
   same input. Schema parity says nothing about it.

A reasonable way to close item 1 or item 3 would be a set of authenticated live requests
against the real service, chosen to discriminate between candidate formulas. None was made:
no paid TypeSafe request was issued during this work, and no TypeSafe subscription is
required to run or test Jevmulator.

---

## 7. Refreshing the pin

Jevmulator does not follow TypeSafe's documentation as it changes, and it does not follow
`jev-latest` automatically. To move the pin:

1. Fetch `https://api.typesafe.ai/openapi.json` into a new dated snapshot and record its
   SHA-256.
2. Diff the component schemas against `contract/sources/openapi.json`.
3. Diff the semantic assertions in this file against the new documentation.
4. Update `CONTRACT_PIN_DATE` and `CONTRACT_OPENAPI_SHA256` in `src/jevmulator/__init__.py`,
   and the hash in `tests/test_schema_conformance.py`.
5. Re-run the conformance, SDK and consumer suites.
6. Record what changed here before adopting it.

`tests/test_schema_conformance.py::TestPinnedBundle` re-hashes the snapshot on every run, so
an edit that skips this procedure fails the suite.


---

## 8. What sys1 changes

`/sys1/v1/systemone` accepts and returns the pinned bodies. What produces the numbers
differs from both Jev and the bare path.

- **The judgment comes from an agent run.** A pi agent on `zai/glm-5.3-flash` reads files,
  and in `prototype-first` runs code, before it submits distributions. Its numbers say what
  that agent concluded after research. They are not Jev's and not a bare model's.
- **Questions share one context.** One run answers every question of a request, so the
  answers can influence each other. The bare path's guarantee 11 does not hold here.
- **Question IDs stay out.** The agent sees `q1..qN` in request order.
- **Derived fields are the daemon's.** The agent submits distributions only. `choice`,
  `score`, `confidence` and `legend` come from the same code as the bare path.
- **The sum limit is stricter.** The form rejects a distribution whose sum is more than
  `JEVMULATOR_SYS1_MAX_SUM_ERROR` (0.01) from 1, and lets the agent correct it. Inside the
  limit, rescaling follows `JEVMULATOR_NORMALIZE_PROBABILITIES` as on the bare path.
- **Latency is minutes.** Jev answers in about 200 ms; a sys1 run takes as long as the
  agent's research, up to the run timeout.
- **Usage counts the agent's model calls.** `usage` sums the tokens pi reports for every
  model call of the run: input plus cache reads and writes, and output. A call reported
  with no count, or with a count of zero, marks the usage partial; nothing is invented.
- **Identical requests share one run.** Same profile, state and questions in the same
  order attach to the run in progress or get its outcome again for a while. Jev answers
  each request separately.
- **Model names are sys1's.** `GET /sys1/v1/models` lists profiles, and every response
  names the profile, the harness and the harness model in `model`.
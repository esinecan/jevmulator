# Jevmulator design and test plan

Pinned contract: the `CONTRACT-2026-09-22.md` snapshot, TypeSafe OpenAPI 3.1.0,
`info.version` 0.2.0, SHA-256 `a191f8a7df6bd6fedced8120dd0fd106f88575d1d1c8360d08900a6c7c0360d5`.

Jevmulator is a local HTTP daemon. It serves the pinned TypeSafe Jev wire surface and
evaluates each question with an OpenAI-compatible upstream model. The initial upstream is
GLM Flash. Jevmulator does not use TypeSafe weights and does not claim TypeSafe judgments.

## 1. Language and dependency choice

The daemon is written in Python 3.11 or later.

Python is chosen for three recorded reasons. The official TypeSafe Python SDK 0.7.1 is the
primary conformance client and runs in the same interpreter for fixture work. The official
adapter that documents the confidence algorithms is Python. The host already runs Python
3.14.0 with pip 25.2.

The runtime has **zero third-party dependencies**. The HTTP server is
`http.server.ThreadingHTTPServer`. The upstream client is `urllib.request`. Concurrency is
`concurrent.futures.ThreadPoolExecutor`. A clean clone therefore installs and starts
without a package index, and no dependency drift can reach the published wire surface.

Test dependencies are pinned separately in `constraints-dev.txt`: `pytest`, `jsonschema`,
and the pinned official client `typesafe-sdk==0.7.1`. The JavaScript leg pins
`@typesafe-ai/sdk@0.6.0` in `tests/js/package.json`.

The official adapter `system-one-adapter-python` is **not vendored and not depended on**.
The captured snapshot contains no LICENSE file, so its licence is unknown. Its two
published confidence formulas are re-implemented in `src/jevmulator/primitives.py` as an
explicitly labelled compatibility policy with attribution to the commit.

## 2. Public HTTP surface

| Method and path | Purpose |
|---|---|
| `POST /v1/systemone` | Pinned evaluation endpoint. Request `SystemOneRequest`, success `SystemOneResponse`. |
| `GET /v1/models` | Pinned discovery endpoint. Success `ModelMetadataList`. |
| `GET /_jevmulator/health` | Local operational route. Not part of the pinned surface. |
| `GET /_jevmulator/status` | Local configuration summary without secrets. Not part of the pinned surface. |
| `GET /_jevmulator/debug/upstream-calls` | Recorded upstream payloads. Only present when `JEVMULATOR_DEBUG_RECORD=1`. |

The pinned paths keep the pinned bodies exactly. Every operational route sits under the
`/_jevmulator/` prefix, so no pinned path is overloaded.

## 3. Authentication

Both pinned endpoints require `Authorization: Bearer <token>`. That token is the daemon's
own credential, read from `JEVMULATOR_API_KEY`. It is separate from every upstream
credential and is never sent upstream.

When `JEVMULATOR_API_KEY` is unset, the daemon generates a 32-byte random token at startup
and writes it to the runtime state file `.jevmulator/runtime.json`. The daemon is therefore
never unauthenticated. The comparison uses `hmac.compare_digest`.

The operational routes under `/_jevmulator/` do not require the bearer token, because they
must answer while the daemon is still starting. They never return a secret.

## 4. Model aliases and identity

`GET /v1/models` lists four names. Each description states that the answer comes from the
configured upstream model and not from TypeSafe weights.

| Name | Role |
|---|---|
| `jev-latest` | Accepted for drop-in compatibility. Both official SDKs insert this default before transmission. |
| `jev-preview` | Accepted for drop-in compatibility. |
| `jevmulator-latest` | Native alias. |
| `jevmulator-<version>-<upstream model>` | The resolved versioned identity, for example `jevmulator-0.1.0-glm-5.3-flash`. |

Every successful response reports the resolved versioned identity in `model`. A caller can
therefore always read which upstream model answered. `release_date` is the contract pin
date `2026-09-22`.

An unrecognised model name returns 422 by default. `JEVMULATOR_UNKNOWN_MODEL=accept`
relaxes that to acceptance. TypeSafe's own unknown-model behaviour is unverified and stays
an open gap.

## 5. Upstream configuration

| Variable | Default | Meaning |
|---|---|---|
| `JEVMULATOR_PROVIDER` | `openai` | `openai` for any OpenAI-compatible Chat Completions endpoint; `fake` for the deterministic offline provider. |
| `JEVMULATOR_UPSTREAM_BASE_URL` | `https://api.z.ai/api/coding/paas/v4` | Upstream root. `/chat/completions` is appended. |
| `JEVMULATOR_UPSTREAM_MODEL` | `glm-5.3-flash` | Exact upstream model. Never substituted silently. |
| `JEVMULATOR_UPSTREAM_API_KEY_ENV` | `ZAI_API_KEY` | Name of the environment variable holding the upstream key. |
| `JEVMULATOR_UPSTREAM_API_KEY` | unset | Direct key value, used only when the referenced variable is absent. |

The key is referenced by variable name by default, so a configuration file can be read and
printed without exposing a secret. No code path logs a key, and `/_jevmulator/status`
reports only whether a key is present.

GLM specifics are explicit provider extensions, not hidden behaviour.
`JEVMULATOR_UPSTREAM_THINKING=disabled` sends `{"thinking": {"type": "disabled"}}`, the
non-thinking mode of the z.ai Chat Completions extension. Setting it to `omit` removes the
field for endpoints that reject it.

Structured output mode is `JEVMULATOR_RESPONSE_FORMAT`: `json_schema` (default, strict JSON
Schema), `json_object`, or `none` (prompted JSON with code-fence stripping). All three paths
validate the decoded object with the daemon's own validator.

## 6. Question isolation

The pinned semantics say questions are independent, and that question IDs are routing
labels that are not supplied to the model.

Jevmulator issues **one upstream call per question**. The upstream payload contains the
state, that question's instructions and that question's criteria. It never contains the
question ID, any other question, or any other answer. The upstream answer key is the fixed
literal `answer`.

This differs from the official adapter, which builds one output model whose fields are
aliased to the caller's question IDs and sends one collective call
(`_schema.py:create_llm_output_model`, `_client.py:_prepare`). That design makes IDs model
input and couples the questions. The difference is recorded in `docs/compatibility.md`.

Isolation is tested by recording the actual upstream payloads and asserting that no
question ID appears in them. It is not tested by comparing output values, which could
match by chance.

## 7. Primitive computation

The daemon asks the upstream model only for a distribution. It computes every derived field
itself.

- **noul**: upstream returns `{"p_yes": number}`. The daemon requires a finite number in
  `[0, 1]`. The wire answer is `{"type": "noul", "noul": p_yes}`. There is no confidence
  field, matching the pinned schema.
- **choice**: upstream returns `{"probabilities": {label: number}}` over exactly the
  supplied criteria labels. The daemon requires exact key coverage, finite values and no
  unknown label. `choice` is the argmax. Ties resolve to the earliest label in the request's
  criteria order. That tie rule is a Jevmulator policy, because TypeSafe's rule is
  undocumented.
- **score**: upstream returns `{"probabilities": {"0": number, ...}}` over the zero-based
  levels. `score` is `sum(i * p_i)` over the rescaled distribution. `legend` repeats each
  original level description with its structure preserved. `probabilities` uses the same
  string keys as `legend`.

Confidence is computed from the distribution with the two algorithms published by the
official adapter at commit `e1d4cc938204b22fc5a3c3aca7044072fe3f712d`:

- choice: `(max(p) - 1/N) / (1 - 1/N)`, and `1.0` when `N == 1`.
- score: first modal index `m`, `D = sum(p_i * |i - m|)`, `U = mean(|i - (N-1)/2|)`, result
  `max(0, 1 - D/U)`, and `1.0` when `N == 1`.

These are **verified official adapter algorithms, not verified TypeSafe production
algorithms**. `docs/compatibility.md` records that distinction.

Normalisation follows the adapter's published tolerance `1e-6`. A distribution whose sum
differs from one by more than that tolerance is rescaled when
`JEVMULATOR_NORMALIZE_PROBABILITIES=1`, which is the default, and the emitted
`probabilities` are the rescaled values. A zero total is rejected instead of being replaced
by a uniform distribution, because a zero total from a language model is a failed answer
rather than a neutral one. No rounding is applied, because TypeSafe's precision and rounding
mode are unverified.

## 8. Failure policy

A failed evaluation never becomes an invented successful judgment.

| Condition | Result |
|---|---|
| Malformed upstream JSON, missing key, unknown candidate, nonfinite value, out-of-range value, zero-total distribution | Up to `JEVMULATOR_REPAIR_RETRIES` corrective re-asks, then HTTP 502 with `error_type` `upstream_invalid_output`. |
| Upstream refusal | HTTP 502 with `error_type` `upstream_refusal`. |
| Upstream connection error, 408, 5xx, 429 | Up to `JEVMULATOR_UPSTREAM_RETRIES` transport retries with exponential backoff and `retry-after` support. |
| Upstream 429 after retries | HTTP 429, `retry-after` forwarded when upstream supplied it. |
| Upstream 503 or 529 after retries | HTTP 529, `error_type` `overloaded`. |
| Other upstream 5xx or 4xx after retries | HTTP 502, `error_type` `upstream_error`. |
| Upstream or whole-request deadline exceeded | HTTP 504, `error_type` `upstream_timeout`. Outstanding work is cancelled. |
| Daemon in-flight limit exceeded | HTTP 429, `error_type` `too_many_requests`. |
| Request body larger than `JEVMULATOR_MAX_BODY_BYTES` | HTTP 413, `error_type` `request_too_large`. |
| Approximate size guard exceeded, when enabled | HTTP 422, `error_type` `max_tokens_exceeded`. |

422 uses the pinned `HTTPValidationError` array shape. Every other error body is
`{"detail": {"error_type": "<code>", "message": "<text>"}}`. The object form satisfies the
retained `jev-review` consumer, which reads `detail.error_type`, and the official SDK error
parsers, which accept an object `detail`. TypeSafe's exact error strings are unverified, so
this is a Jevmulator policy.

## 9. Token accounting

The wire schema requires integer `input_tokens` and `output_tokens`. Jevmulator reports the
sum of the counts the upstream provider actually returned across that request's per-question
calls. These are **upstream GLM tokens**, not TypeSafe tokens, and the two tokenizers differ.

Honesty about missing counts is handled by `JEVMULATOR_USAGE_POLICY`:

- `upstream`, the default: the body carries the sum of the counts actually reported. When
  any call reported no usage, the response header `X-Jevmulator-Usage-Source` reads
  `upstream-partial` instead of `upstream`, and the shortfall is counted in
  `/_jevmulator/status`. No count is invented.
- `strict`: a missing upstream count fails the request with HTTP 502 and `error_type`
  `usage_unavailable`, rather than emitting an unbacked integer.

Headers sit outside the pinned body schema, so no consumer is affected by the signal.

## 10. Concurrency, retries and cleanup

`JEVMULATOR_MAX_UPSTREAM_CONCURRENCY`, default 4, bounds simultaneous upstream calls per
process. `JEVMULATOR_MAX_INFLIGHT_REQUESTS`, default 16, bounds simultaneous HTTP requests.
`JEVMULATOR_UPSTREAM_TIMEOUT_SECONDS`, default 60, bounds one upstream call.
`JEVMULATOR_REQUEST_TIMEOUT_SECONDS`, default 120, bounds the whole evaluation.

When the request deadline passes, remaining question futures are cancelled, running upstream
sockets are closed by their own timeout, and the executor is not leaked. Server shutdown
joins the executor with a bounded timeout.

## 11. Lifecycle scriptlet

`jevmulator.ps1` supports `start [-Port N]`, `status`, `stop` and `restart` on Windows
PowerShell 5.1.

- The default port is **8769**. Ports 8765, 8766, 8767, 8787, 8790 and 8791 are already bound
  on this host and are not used.
- `start` writes `.jevmulator/runtime.json` holding the process id, the port, the process
  start time and the base URL, then polls `/_jevmulator/health` until ready or until the
  readiness timeout. It returns only after readiness, and returns a nonzero exit code with a
  message on failure.
- The chosen port is remembered in the runtime file, so `status` and `stop` need no `-Port`.
  An explicit `-Port` still overrides.
- `stop` verifies **both** the process id and the recorded process start time before
  terminating, so a reused process id belonging to an unrelated process is never killed. A
  stale runtime file is detected and removed.
- An occupied port is detected before launch and reported, instead of producing a
  half-started daemon.
- The background process starts with a hidden window and its output redirected to
  `.jevmulator/daemon.out.log` and `.jevmulator/daemon.err.log`.
- All file input and output uses explicit UTF-8 without a byte order mark.

## 12. Test matrix

| Suite | File | What it establishes |
|---|---|---|
| Primitives | `tests/test_primitives.py` | Confidence formulas, expected score, normalisation, argmax and tie rule, numerical edges. |
| Wire validation | `tests/test_wire.py` | Request acceptance and rejection against the pinned schema, optional and null instructions, structured content, one-level score, null choice descriptions. |
| Schema conformance | `tests/test_schema_conformance.py` | Every emitted response validated with `jsonschema` against `contract/schemas/wire-*.json`, plus the eight official documentation examples. |
| HTTP integration | `tests/test_http.py` | Real loopback daemon: auth, invalid JSON, 422 shape, unknown model, discovery, oversize body, concurrent requests, resource cleanup. |
| Upstream failures | `tests/test_upstream_failures.py` | Controlled fake upstream HTTP server: invalid output, refusal, timeout, 429, 5xx, 529, retry bounds, repair bounds. |
| Isolation | `tests/test_isolation.py` | Metamorphic: rename IDs, add unrelated questions, reorder questions; assert the recorded upstream payload for a question is identical and contains no ID. |
| Official SDKs | `tests/test_sdk_python.py`, `tests/js/run.mjs` | Pinned `typesafe-sdk==0.7.1` and `@typesafe-ai/sdk@0.6.0` against the real daemon. |
| Retained consumers | `tests/test_consumers.py` | The `jev-ultrafast` choice validator and the `jev-review` response shape re-expressed as checks. |
| Lifecycle | `tests/test_lifecycle.py` | `jevmulator.ps1` on this host: default and custom port, readiness, duplicate start, stop twice, port collision, stale runtime file, unrelated-process protection, path with a space. |
| Live GLM Flash | `tests/live/test_glm_flash.py` | Bounded real calls, marked and skipped by default. |

No test calls a live provider unless `JEVMULATOR_LIVE_TESTS=1` is set. The default provider
inside tests is `fake`, or a local fake HTTP upstream.

## 13. What stays unknown

These are recorded as unknown. None of them is asserted as TypeSafe behaviour in the tests
or in the README.

1. TypeSafe's production confidence formula, numeric precision, rounding mode and choice tie
   rule.
2. TypeSafe's runtime acceptance of a one-level score, a zero-option choice and empty
   content.
3. TypeSafe's exact 401, 422, 429 and 529 bodies, headers and unknown-model behaviour.
4. TypeSafe's tokenizer and billing counts.
5. Whether GLM Flash judgments agree with Jev judgments. Schema parity is not calibration
   parity.

## 14. Authorization

The implementation brief at
`~/agent-personal-space/jevmulator/implementation-brief.md` records Eren's
explicit authorization for this implementation, for GLM Flash backed testing, and for
creating and pushing the private repository `esinecan/jevmulator`. This design introduces no
further approval gate.

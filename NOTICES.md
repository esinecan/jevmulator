# Third-party material and provenance

Jevmulator's own source carries the licence in `LICENSE`. The files below are not
original work, are included for conformance testing, and remain subject to the rights of
their owners.

## TypeSafe published API document

`contract/sources/openapi.json` is TypeSafe's published OpenAPI document, retrieved from
<https://api.typesafe.ai/openapi.json> on 2026-09-22.

- OpenAPI 3.1.0, `info.version` 0.2.0
- SHA-256 `a191f8a7df6bd6fedced8120dd0fd106f88575d1d1c8360d08900a6c7c0360d5`

It is the authoritative wire contract this daemon targets. `contract/schemas/wire-*.json`
wrap its components as Draft 2020-12 documents without adding semantic limits.
`contract/schemas/official-sdk-*.json` are extracted from the official Python SDK's public
types and are a separate, non-authoritative layer.

## TypeSafe documentation examples

`contract/fixtures/official-doc-example-*.json` are request and response examples copied
from TypeSafe's public API documentation. They are **documentation examples, not observed
traffic**. Some of their confidence numbers do not equal any published formula, so they
cannot settle production numeric behaviour.

## TypeSafe historical recorded traffic

`contract/fixtures/recorded-historical-typesafe-{request,response}.json` are extracted
from the committed test cassette in `typesafe-ai/system-one-adapter-python` at commit
`e1d4cc938204b22fc5a3c3aca7044072fe3f712d`. They record model `speed_latest` and response
model `speed_v12_snowy_flower`, carry `stats: {}` per answer and `assets_used: null`, and
embed no capture date.

They are **historical**. They are not a current Jev 1.13 capture, and their extra fields
are not treated as current required fields anywhere in this repository.

## Official adapter algorithms

No file from `typesafe-ai/system-one-adapter-python` is vendored into this repository. The
captured snapshot of that repository contains no LICENSE file, so its licensing terms are
unknown to this project.

`src/jevmulator/primitives.py` re-implements the two confidence formulas that adapter
publishes in `src/system_one_adapter/_utils/confidence_metrics.py` at commit
`e1d4cc938204b22fc5a3c3aca7044072fe3f712d`:

- choice: `(max(p) - 1/N) / (1 - 1/N)`
- score: `max(0, 1 - D/U)` with `D = sum(p_i * |i - m|)` and `U = mean(|i - (N-1)/2|)`

They are used as an explicitly documented compatibility policy. They are verified adapter
algorithms. They are **not** verified TypeSafe production algorithms.

## Official client SDKs

The test suite installs the pinned official clients from their public registries. Neither
is vendored.

- `typesafe-sdk==0.7.1` from PyPI, pinned at commit `0ffd094c72ed9445223060b24ffd7a56aa781fb4`
- `@typesafe-ai/sdk@0.6.0` from npm, pinned at commit `66880ccded6cb642dc1809620c2b108c33730214`

## Process containment for sys1

`src/jevmulator/sys1/jobs.py` ports the kill-on-close job-object pattern (start suspended,
assign to the job, resume with `NtResumeProcess`) from the `Job` class in Eren Sinecan's
own `agent-personal-space/experiments/tui-mcp-eval-2026-09-22/bundle/core.py`. The port
drops that class's `psutil` dependency: the job's process list and active-process count
are read with `QueryInformationJobObject` instead.

## pi

sys1's first harness is the `pi` coding agent (`@earendil-works/pi-coding-agent`), which
the daemon runs as an installed program. No pi file is vendored. `pi_judge.ts` is this
project's own extension, written against pi 0.85.1's extension API.

## Name

"TypeSafe" and "Jev" are the provider's names. This project is an independent emulator of
a published HTTP contract. It is not affiliated with, endorsed by, or connected to
TypeSafe, and it does not use TypeSafe model weights.

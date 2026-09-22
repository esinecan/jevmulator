# Test report

Run on 2026-09-22 on the host described below. Every command and every count here was
executed, not estimated.

## Environment

| Item | Value |
|---|---|
| Operating system | Windows 11 Home, 10.0.26200 |
| Python | 3.14.0, pip 25.2 |
| Node | v24.11.1, npm 11.6.3 |
| pytest | 8.3.4 |
| jsonschema | 4.23.0 |
| Official Python SDK | `typesafe-sdk==0.7.1` (commit `0ffd094c72ed9445223060b24ffd7a56aa781fb4`) |
| Official JavaScript SDK | `@typesafe-ai/sdk@0.6.0` (commit `66880ccded6cb642dc1809620c2b108c33730214`) |
| Upstream under test | `glm-5.3-flash` at `https://api.z.ai/api/coding/paas/v4` |
| Contract snapshot | OpenAPI 3.1.0, `info.version` 0.2.0, SHA-256 `a191f8a7df6bd6fedced8120dd0fd106f88575d1d1c8360d08900a6c7c0360d5` |

## Commands and results

```powershell
python -m pip install -e ".[dev]" -c constraints-dev.txt
cd tests\js; npm ci; cd ..\..

python -m pytest tests/ -q -m "not windows and not live"
# 339 passed, 1 skipped, 27 deselected in 23.63s

python -m pytest tests/test_lifecycle.py -q
# 27 passed in 224.52s

$env:JEVMULATOR_LIVE_TESTS = "1"
python -m pytest tests/live -q -s
# 12 passed in 28.36s
```

The one skip is `tests/live/test_glm_flash.py`, which declines to run unless
`JEVMULATOR_LIVE_TESTS=1` is set. The 27 deselected are the Windows lifecycle cases.

**Total: 378 tests, all passing.**

## Per-suite counts

| Suite | Cases | What it establishes |
|---|---|---|
| `tests/test_primitives.py` | 54 | Both confidence formulas, expected score, normalisation, argmax and the tie rule, and the numerical edges. |
| `tests/test_wire.py` | 55 | Request acceptance and rejection against the pinned schema. |
| `tests/test_prompts.py` | 28 | Prompt construction, including the option-naming defect the live run found. |
| `tests/test_schema_conformance.py` | 33 | `jsonschema` validation against `contract/schemas/wire-*.json`, plus the eight documentation examples and the historical cassette. |
| `tests/test_http.py` | 53 | Real loopback HTTP: auth, malformed input, limits, headers, concurrency, cleanup. |
| `tests/test_upstream_failures.py` | 46 | A controlled fake upstream HTTP server: invalid output, refusal, status mapping, retry and repair bounds, timeouts. |
| `tests/test_isolation.py` | 21 | Question independence and question-ID invisibility, proved on recorded upstream payloads. |
| `tests/test_consumers.py` | 23 | The retained raw consumers re-expressed as validators. |
| `tests/test_sdk_python.py` | 21 | The pinned Python SDK against the real daemon. |
| `tests/test_sdk_js.py` | 5 | The pinned JavaScript SDK against the real daemon, wrapping 15 JavaScript cases in `tests/js/run.mjs`. |
| `tests/test_lifecycle.py` | 27 | `jevmulator.ps1` on this host. |
| `tests/live/test_glm_flash.py` | 12 | Bounded real calls to `glm-5.3-flash`. |

## No test reaches a live provider by default

`tests/conftest.py` clears every `JEVMULATOR_*` variable before each test and sets
`JEVMULATOR_PROVIDER=fake`. The failure suites speak HTTP to a fake upstream server bound
to `127.0.0.1` on an ephemeral port. `tests/live` skips at module level unless
`JEVMULATOR_LIVE_TESTS=1` is set.

## Behaviour, not mirrored implementation

Several checks would pass trivially if they only re-read the implementation. They do not.

- **Isolation** compares the real upstream payloads recorded through
  `/_jevmulator/debug/upstream-calls`, not the answers. Question IDs are random tokens
  (`qid` plus 16 hex characters) that cannot appear in the state, instructions or criteria,
  so a substring search is meaningful. An early smoke check that used readable IDs such as
  `billing` produced a false positive, because `billing` also appears in the caller's own
  instruction text. That is why the suite uses random tokens.
- **Consumer validators** are the retained clients' own rules, re-expressed. Each one is
  itself tested against a deliberately broken input, so a validator that always passed
  would fail its own test.
- **Confidence formulas** are asserted against the published formula recomputed inline in
  the test, and against the documented worked example (`{"0": 0.1, "1": 0.1, "2": 0.8}`
  gives score 1.7).
- **Lifecycle** runs the real `jevmulator.ps1` in a real PowerShell process, in a temporary
  directory whose path contains a space.

## Fixture provenance

Three layers are kept apart and are never mixed.

| Layer | Files | Standing |
|---|---|---|
| Official documentation examples | `contract/fixtures/official-doc-example-0*.json` (8 files) | Documentation examples. Not observed traffic. They settle no runtime question. |
| Historical recorded traffic | `contract/fixtures/recorded-historical-typesafe-*.json` | Extracted from the adapter's committed cassette. Model `speed_latest`, response model `speed_v12_snowy_flower`. Not a current Jev 1.13 capture. Its extra fields (`stats`, `assets_used`) are asserted **not** to be required. |
| Locally synthesized | Everything this daemon produces in the suites | Synthetic. Never presented as provider output. |

## Defects found by the tests

Every one of these was found by a test and then fixed. Each has a regression case.

### 1. Early error responses reset keep-alive connections

`tests/test_http.py::TestAuthentication::test_missing_header_is_401` and
`TestBodyLimits::test_a_body_over_the_limit_is_413` failed with `WinError 10053` and
`WinError 10054`.

The daemon answered 401 and 413 before reading the request body. The client was still
writing, so Windows reset the connection. `Handler._drain_request_body` now reads and
discards an unread body up to 1 MiB before any early response, and closes the connection
instead for anything larger. The two cases were then run five times in a row to confirm the
fix was not timing luck.

### 2. A dropped client socket printed a server traceback

Node's fetch agent closes its keep-alive sockets when the process exits. Each one produced
a `ConnectionResetError` traceback on the daemon's standard error.
`JevmulatorServer.handle_error` now logs a client disconnect at debug level, and
`Handler.handle_one_request` swallows it. Covered by
`tests/test_http.py::TestClientDisconnect`.

### 3. The JavaScript runner aborted Node on Windows

`run.mjs` called `process.exit()` while sockets were open, and Node exited with
`0xC0000409`. It now sets `process.exitCode` and lets the runtime drain.

### 4. A module-scoped fixture raced the environment fixture

`tests/test_sdk_js.py` uses a module-scoped fixture, which pytest builds before any
function-scoped fixture runs. The daemon therefore generated a random API key that the
client never saw, and every JavaScript case failed with 401. `start_daemon` now sets its
own key rather than relying on the ambient environment.

### 5. The lifecycle harness hung on an inherited pipe

`tests/test_lifecycle.py` stalled for 18 minutes with no output. `Workspace.run` used
`subprocess.run(capture_output=True)`. The hidden background daemon inherited that stdout
pipe, so `communicate()` never returned even after the timeout killed PowerShell itself.
The harness now captures through files, and the process liveness check uses
`OpenProcess`/`GetExitCodeProcess` instead of starting a new PowerShell each time. Recorded
in `run/exclusions.json`; no unit was excluded, because the cause was found and fixed.

### 6. `Start-Process` split a path containing a space

`jevmulator.ps1` passed `--state-dir "<path with a space>"` through
`Start-Process -ArgumentList`, which joins its array with spaces and quotes nothing. The
daemon received two arguments and exited with `unrecognized arguments`. Every argument now
goes through `ConvertTo-ProcessArgument`, which quotes and escapes it. Covered by
`TestPathsWithSpaces`.

### 7. Documented exit codes never reached the caller

`TestPortCollision::test_an_occupied_port_is_reported_before_launching` expected exit code
2 and got 1. The script sets `$ErrorActionPreference = 'Stop'`, so `Write-Error` is a
terminating error and aborted the function before its `return 2`. All four failure paths now
use `Write-Failure`, which writes to the error stream without terminating.

### 8. An unusable interpreter raised a raw PowerShell exception

`TestPortCollision::test_a_readiness_failure_returns_its_own_code` passed
`-Python not-a-python.exe` and got exit 1 with a `Start-Process` stack trace.
`Start-Process` is now wrapped, and reports exit code 3 with a readable message.

### 9. An option's description leaked into its probability key

The live GLM Flash run failed one case with
`the answer left out requested candidates: 'angry', 'excited'`.

The prompt rendered each option as `- angry: An upset or hostile message`. The model read
the whole line as the option name and returned the key `"angry: upset"`. The daemon rejected
it correctly as an unknown candidate, so no invented answer was produced. The prompt now
quotes the option name on its own line, puts the description on the next line, and states
the exact key list as a JSON array.

A direct probe confirmed both the defect and the fix:

```
before:  {"answer": {"probabilities": {"angry: upset": 0.1, "calm": 0.85,
                                       "excited: {\"note\": \"eager\"}": 0.05}}}
after:   {"answer": {"probabilities": {"angry": 0.2, "calm": 0.75, "excited": 0.05}}}
```

`tests/test_prompts.py` adds 28 cases covering this class of ambiguity, including an option
name that itself contains a colon.

## Live GLM Flash run

Command:

```powershell
$env:ZAI_API_KEY = "<set in the environment>"
$env:JEVMULATOR_LIVE_TESTS = "1"
python -m pytest tests/live -q -s
```

Result: **12 passed in 28.36s**. Evidence written to `live-evidence/glm-flash-run.json`,
which git ignores.

| Measure | Value |
|---|---|
| Upstream model | `glm-5.3-flash` |
| Upstream calls spent | 16 of a 30 call ceiling |
| Requests | 11 |
| Requests returning 200 | 10 of the 10 intended; the 11th expects a failure and got one |
| Upstream input tokens | 2722 |
| Upstream output tokens | 331 |
| Wall time, all requests | 28.06s |
| Slowest request | 7.49s (four questions, four upstream calls) |
| Judgment checks | 7 |
| Judgment agreements | 7 of 7 |

Per request:

| Case | Status | Upstream calls | Seconds | Input tokens | Output tokens | Judgment |
|---|---|---|---|---|---|---|
| `billing-noul-yes` | 200 | 1 | 1.64 | 132 | 14 | `noul` 1.0, above 0.5 as expected |
| `billing-noul-no` | 200 | 1 | 3.21 | 130 | 14 | `noul` 0.0, below 0.5 as expected |
| `tone-choice-angry` | 200 | 1 | 2.62 | 250 | 36 | chose `angry` as expected |
| `tone-choice-excited` | 200 | 1 | 1.77 | 250 | 36 | chose `excited` as expected |
| `urgency-score-high` | 200 | 1 | 1.85 | 240 | 33 | score 1.95, above 1.0 as expected |
| `urgency-score-low` | 200 | 1 | 2.17 | 241 | 33 | score 0.0, below 1.0 as expected |
| `mixed-structured` | 200 | 3 | 2.40 | 700 | 83 | `noul` 1.0, above 0.5 as expected |
| `model-identity` | 200 | 1 | 1.72 | 121 | 14 | not a judgment case |
| `usage-reporting` | 200 | 1 | 2.94 | 126 | 14 | not a judgment case |
| `concurrency-4` | 200 | 4 | 7.49 | 532 | 54 | not a judgment case |
| `unknown-upstream-model` | 502 | 1 | 0.25 | none | none | expected failure, no answer returned |

**Schema validity and judgment correctness are separate.** All ten successful requests were
schema valid, which is the daemon's guarantee. All seven judgment expectations agreed,
which is a property of `glm-5.3-flash` on seven synthetic examples and is not a measure of
calibration against Jev.

`glm-5.3-flash` returned `noul` values of exactly 1.0 and exactly 0.0 on the clear cases.
Jev's own values on the same text are unknown, and no claim is made about how the two
compare.

### A note on z.ai strict mode

The daemon sends `response_format: {"type": "json_schema", ..., "strict": true}` with
`required` and `additionalProperties: false`. During the defect-9 probe, z.ai returned an
object carrying keys that the schema did not allow. The endpoint's strict mode did not
enforce the constraint.

The daemon does not rely on it. Every decoded object is validated by the daemon itself, and
an object that fails is rejected, repaired within the retry bound, or turned into a 502.

## Quickstart reproduction in a clean clone

The README quickstart was executed against a fresh `git clone` of the tested commit, into a
directory separate from the working tree, with only `ZAI_API_KEY` and `JEVMULATOR_API_KEY`
set. The commands run were exactly those in the README: `.\jevmulator.ps1 start`, the
`curl.exe` POST, `.\jevmulator.ps1 status`, and `.\jevmulator.ps1 stop`. The transcript is in
[Clean clone quickstart transcript](#clean-clone-quickstart-transcript).

## What the tests do not establish

1. That Jevmulator's confidence numbers equal Jev's. The formulas come from TypeSafe's own
   adapter and are used as a documented policy. TypeSafe's production formula is unverified.
2. That Jevmulator's judgments equal Jev's. They come from a different model.
3. That TypeSafe accepts a one-level score, a zero-option choice or empty content at
   runtime. The schema permits all three and Jevmulator accepts them; TypeSafe's runtime
   behaviour is unverified.
4. That TypeSafe's 401, 429 and 529 bodies match Jevmulator's. Only the 422 body is
   published.
5. That the token counts correspond to TypeSafe's tokenizer or billing. They are upstream
   GLM tokens, summed across one call per question.
6. That a live model is reproducible across runs. The metamorphic cases use the
   deterministic fake provider precisely because a language model at temperature zero is not
   a guarantee of reproducibility, and nothing here claims otherwise.

## Clean clone quickstart transcript

The repository was cloned into `%TEMP%\jevmulator clean clone`, a directory whose path
contains a space. Only `ZAI_API_KEY` and `JEVMULATOR_API_KEY` were set. The commands are the
ones the README gives.

### Install and packaging

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -c constraints-dev.txt
# exit 0

.\.venv\Scripts\jevmulator.exe --version
# jevmulator 0.1.0

cd tests\js; npm ci
# added 1 package in 1s

.\.venv\Scripts\python.exe -m pytest tests/ -q -m "not windows and not live"
# 339 passed, 1 skipped, 27 deselected in 27.65s
```

Wheel build, then installation into a separate empty virtual environment:

```powershell
python -m build --wheel
# Successfully built jevmulator-0.1.0-py3-none-any.whl  (47674 bytes)

python -m pip install dist\jevmulator-0.1.0-py3-none-any.whl
python -m pip list
# Package    Version
# ---------- -------
# jevmulator 0.1.0
# pip        25.2
```

The installed wheel pulls in nothing else. That is the zero-dependency runtime claim
checked rather than asserted.

### Quickstart, against live GLM Flash

```powershell
$env:JEVMULATOR_API_KEY = "local-dev-token"

.\jevmulator.ps1 start
# jevmulator: starting on port 8769 ...
# jevmulator: ready on http://127.0.0.1:8769 as process 20104, upstream model glm-5.3-flash.
# exit 0
```

The `Invoke-RestMethod` form returned:

```json
{
  "model": "jevmulator-0.1.0-glm-5.3-flash",
  "answers": { "billing": { "type": "noul", "noul": 1.0 } },
  "usage": { "input_tokens": 132, "output_tokens": 14 }
}
```

The request-file form with `curl.exe` returned:

```json
{"model": "jevmulator-0.1.0-glm-5.3-flash",
 "answers": {"billing": {"type": "noul", "noul": 0.95}},
 "usage": {"input_tokens": 121, "output_tokens": 14}}
```

```powershell
curl.exe -s http://127.0.0.1:8769/v1/models -H "Authorization: Bearer local-dev-token"
# four models listed, every description naming glm-5.3-flash

.\jevmulator.ps1 status -ShowKey
# ready             : True
# upstream model    : glm-5.3-flash
# reported model    : jevmulator-0.1.0-glm-5.3-flash
# api key           : local-dev-token

.\jevmulator.ps1 stop
# jevmulator: stopped.                        exit 0

.\jevmulator.ps1 stop
# jevmulator: not running. No runtime file.   exit 0
```

### Two more defects the clean clone found

**10. The hand-written dependency lock did not install.**
`constraints-dev.txt` pinned `rpds-py==0.22.3`, which publishes no wheel for Python 3.14. A
clean virtual environment tried to build it from source and failed with `Failed building
wheel for rpds-py`. Those transitive pins were written by hand rather than generated. The
file is now a real freeze of a resolution performed on this interpreter, and it records the
command that regenerates it.

**11. The README quickstart did not work on this host.**
It passed a JSON body inline to `curl.exe`. Windows PowerShell 5.1 removes the double quotes
before the program sees them, and the daemon answered `422` with
`{"loc": ["body"], "msg": "JSON decode error: Expecting property name enclosed in double
quotes...", "type": "json_invalid"}`. The quickstart now uses `Invoke-RestMethod`, with a
request-file form as the alternative. Both were executed above and both returned 200. A
troubleshooting entry in the README names the failure.

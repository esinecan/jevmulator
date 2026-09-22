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
# 354 passed, 1 skipped, 67 deselected in 27.95s

python -m pytest tests/ -q -m "windows"
# 67 passed, 1 skipped, 354 deselected in 272.07s

$env:JEVMULATOR_LIVE_TESTS = "1"
python -m pytest tests/live -q -s
# 12 passed in 28.36s
```

The one skip in each selection is `tests/live/test_glm_flash.py`, which declines to run
unless `JEVMULATOR_LIVE_TESTS=1` is set.

**Total: 433 tests, all passing:** 354 offline, 67 Windows and 12 live.

The live figure is from the run of 2026-09-22 at about 19:36 UTC. It has not been re-run
since, because no further live call was made after the supervisor directive at 20:07 UTC.
The acceptance corrections below touch the scriptlet, the operational routes and the inbound
read path, none of which the live suite exercises.

## Per-suite counts

| Suite | Cases | What it establishes |
|---|---|---|
| `tests/test_primitives.py` | 54 | Both confidence formulas, expected score, normalisation, argmax and the tie rule, and the numerical edges. |
| `tests/test_wire.py` | 55 | Request acceptance and rejection against the pinned schema. |
| `tests/test_prompts.py` | 28 | Prompt construction, including the option-naming defect the live run found. |
| `tests/test_schema_conformance.py` | 33 | `jsonschema` validation against `contract/schemas/wire-*.json`, plus the eight documentation examples and the historical cassette. |
| `tests/test_http.py` | 68 | Real loopback HTTP: auth, malformed input, limits, headers, concurrency, cleanup. |
| `tests/test_upstream_failures.py` | 46 | A controlled fake upstream HTTP server: invalid output, refusal, status mapping, retry and repair bounds, timeouts. |
| `tests/test_isolation.py` | 21 | Question independence and question-ID invisibility, proved on recorded upstream payloads. |
| `tests/test_consumers.py` | 23 | The retained raw consumers re-expressed as validators. |
| `tests/test_sdk_python.py` | 21 | The pinned Python SDK against the real daemon. |
| `tests/test_sdk_js.py` | 5 | The pinned JavaScript SDK against the real daemon, wrapping 15 JavaScript cases in `tests/js/run.mjs`. |
| `tests/test_lifecycle.py` | 34 | `jevmulator.ps1` on this host, including the fail-closed identity checks. |
| `tests/test_identity.py` | 33 | The process-ownership proofs, tested directly against the shared library. |
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
`OpenProcess`/`GetExitCodeProcess` instead of starting a new PowerShell each time.

Two executions of the unchanged unit stalled, so `windows-lifecycle-suite-attempt-1` **is
excluded** from unchanged reattempts in `run/exclusions.json`. The fixed harness is recorded
there as a separate named unit, `windows-lifecycle-suite-attempt-2`, because the
implementation changed. The exclusion of attempt 1 is not lifted by attempt 2 passing: the
unit that passed is a different one. The identity regressions below were added as a third
named unit, `windows-lifecycle-suite-attempt-3`.

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

## Live call budget: the ceiling was exceeded

The brief set an initial ceiling of 30 live calls. **The session made 40 counted calls and
at least 41 actual calls. The ceiling was exceeded by 10.**

Both live runs reported "16 of a 30 call ceiling", and earlier notes repeated that figure.
It was never the session total. The `Budget` object in `tests/live/test_glm_flash.py` is
built at module import, so it resets on every pytest run, and no counter spanned the
session. Reporting a per-run figure as budget compliance was wrong, and the corrected
ledger is below.

| # | Segment | Counted | Actual | Input tokens | Output tokens |
|---|---|---|---|---|---|
| 1 | Recon access probe | 1 | 1 | 17 | 3 |
| 2 | Live run 1, before the prompt fix | 16 | 17 | 1768 | 331 |
| 3 | Defect 9 diagnostic probes | 2 | 2 | not recorded | not recorded |
| 4 | Post-fix verification probe | 1 | 1 | not recorded | not recorded |
| 5 | Live run 2, after the prompt fix | 16 | 16 | 2722 | 331 |
| 6 | PowerShell request-form check | 2 | 2 | 264 | 28 |
| 7 | Clean-clone quickstart | 2 | 2 | 253 | 28 |
| | **Total** | **40** | **41** | **5024 known** | **721 known** |

Two undercounts are known and are not estimated away.

- Live run 1's `mixed-structured` request failed, so no `X-Jevmulator-Upstream-Calls`
  header came back and the harness fell back to the question count of 3. The real cost was
  4: one noul call, two choice attempts including the repair, and one score call.
- `input_tokens_total` and `output_tokens_total` sum successful requests only, and three
  calls printed no usage block at all. The token figures are therefore a floor, not a
  total.

The provider returned no price field on any response. At the published `glm-5.3-flash`
rates of 0.15 per million input tokens and 0.50 per million output tokens, the known 5024
input and 721 output tokens correspond to roughly 0.0011 US dollars. That is arithmetic
from published rates, not a figure the provider reported.

**On timing.** No instruction to stop existed before 20:07 UTC; the supervisor's input sat
unsent in the terminal composer until then. The last live call was made at about 19:46 UTC.
No live call was made after the directive arrived. The overrun is not excused by that: the
harness had no cumulative counter, which is the actual gap.

The full ledger, with the source of every figure, is in
`run/live-call-ledger.json` outside this repository.

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

---

## Acceptance review corrections

An independent code review after the first publication found three defects. Each is fixed
and each has regression cases. No live provider call was made to find or fix any of them.

### 12. The scriptlet's process identity check failed open

`Get-DaemonProcess` in `jevmulator.ps1` accepted a runtime file with **no** recorded
creation time, accepted a creation time that would not parse, accepted an **unreadable**
command line, and accepted any command line merely containing the word `jevmulator`. Each
of those paths let `stop` hand a process to `Stop-Process` without proof that this checkout
owned it. The last one would also match a Jevmulator daemon belonging to a different
checkout on the same machine.

The check now **fails closed**. It returns the process only when three proofs all hold:

1. the process id exists,
2. the recorded creation time is present, parses, and matches the live process within two
   seconds,
3. the command line is readable, invokes `-m jevmulator serve`, **and** names this
   checkout's state directory.

If any proof cannot be obtained, the function returns nothing and sets
`$script:IdentityReason`, which `status` and `stop` print. Nothing is terminated.

One consequence is deliberate. A daemon started by hand with `python -m jevmulator serve`
writes no creation time, so the scriptlet will refuse to stop it and will say why. Stop
such a daemon with Ctrl+C.

`tests/test_lifecycle.py::TestIdentityCheckFailsClosed` adds seven cases: a missing
creation time, a malformed creation time, a mismatched creation time, a live process that
is not a daemon, a real Jevmulator daemon belonging to a **different checkout**, the
refusal reason appearing in `status`, and a properly verified daemon still being stopped.
No case terminates an unrelated process; each case kills only the process it created.

### 13. The debug recording route needed no credential

`GET` and `DELETE /_jevmulator/debug/upstream-calls` did not call `_require_auth`. That
recording holds the caller's state, the caller's instructions and the whole prompts sent
upstream, so anyone who could reach the loopback port could read them.

Both methods now require the daemon bearer token, and the check runs **before** the
recording flag is consulted, so an unauthenticated caller does not even learn whether
recording is on. `/_jevmulator/health` and `/_jevmulator/status` stay open: readiness
polling must work before a caller holds a key, and neither route returns a secret or any
caller content.

`tests/test_http.py::TestDebugRouteRequiresAuth` adds seven cases, including one that
asserts an unauthenticated reader never sees a prompt body.

### 14. Inbound socket reads were unbounded

`_read_body` called `self.rfile.read(length)` and `_drain_request_body` looped without a
deadline. A client that declared a `Content-Length` and then stopped sending occupied one
handler thread for as long as it liked. The evaluation deadline never applied, because the
request never reached the evaluator.

Three changes fix it. `Handler.setup` sets a socket timeout from
`JEVMULATOR_INBOUND_TIMEOUT_SECONDS`, default 30 seconds. Both read paths now loop in
bounded chunks under one overall deadline, so a client that trickles one byte at a time
cannot reset the timeout forever. An incomplete body returns **408** with `error_type`
`request_timeout` and closes the connection, and an idle keep-alive connection is closed
quietly rather than raising.

`tests/test_http.py::TestIncompleteRequestBody` adds five cases: a partial body answered
with 408 under a one-second timeout, a healthy request served afterwards on the same
daemon, a stalled client that does not consume the in-flight budget, a client that
disconnects mid-body without producing a server error, and the setting appearing in
`/_jevmulator/status`.

### Counts after the corrections

| Suite | Before | After |
|---|---|---|
| `tests/test_http.py` | 53 | 65 |
| `tests/test_lifecycle.py` | 27 | 34 |
| Offline selection | 339 passed, 1 skipped | 351 passed, 1 skipped |

Command and result:

```powershell
python -m pytest tests/ -q -m "not windows and not live"
# 354 passed, 1 skipped, 67 deselected in 27.95s
```

---

## Second acceptance review

An independent rerun and two targeted probes found one test defect and two implementation
bugs in commit `dda6296`. All three are fixed. No live provider call was made.

### 15. The 408 test read only the response headers

The independent rerun reported `350 passed, 1 failed, 1 skipped`. The failure was
`TestIncompleteRequestBody::test_an_incomplete_body_is_answered_with_408`, which stopped
reading at the blank line after the headers and then asserted on the body. Headers and body
are separate writes and can arrive in separate reads, so the case was flaky.

**This was a test defect, not evidence that the 408 was absent.** The case now reads the
declared `Content-Length` body, or to end of stream, through a `_read_whole_response`
helper before asserting.

### 16. The whole-body deadline did not bound a trickling client

`rfile.read(n)` is a buffered read. It loops over several underlying receives without
returning, so the deadline check placed around that call never ran, and each trickled byte
reset the per-socket timeout. The bound existed only against a client that went completely
silent.

An independent probe set `inbound_timeout=0.4`, declared `Content-Length: 100`, and sent one
space every 0.1 seconds. The server was still reading at 1.0 seconds, **2.5 times the
deadline**.

`Handler._read_bounded` now reads through `read1`, which returns after **one** underlying
receive, and sets the socket timeout to the remaining budget before each one. Both
`_read_body` and `_drain_request_body` go through it, so the total is bounded by one
absolute deadline however the client paces its bytes. The socket timeout is restored in a
`finally`, so the response write and the next keep-alive request line do not inherit a
nearly expired budget.

The same probe against the fixed build:

```
inbound_timeout=0.4s, Content-Length=100, one space per 0.1s
  server acted after 0.47s -> connection reset by server
  daemon still healthy afterwards: 200
```

`tests/test_http.py::TestTricklingClientIsBounded` adds three cases: a trickling client
answered or closed inside 1.5 seconds against a 0.4 second deadline, a healthy request
served afterwards on the same daemon, and four simultaneous tricklers that do not exhaust
the handlers.

One transport detail is asserted honestly. When the daemon closes while the client still has
bytes in flight, Windows answers with a reset, and the already-written 408 body can be
discarded. The property under test is that the handler **stopped**, so the case accepts
either a 408 or a prompt close, and asserts the elapsed bound in both. A client that stops
sending does receive the 408; that is the separate `TestIncompleteRequestBody` case.

### 17. The state-directory comparison accepted a prefix collision

`$commandLine.IndexOf($StateDir)` matched any substring. A daemon owning
`C:\probe\.jevmulator-other` therefore satisfied a check for `C:\probe\.jevmulator`, and the
expected path appearing inside an unrelated argument also satisfied it. An independent
non-destructive probe at `run/probe-identity.ps1` demonstrated the first case.

The comparison now parses the command line into arguments and compares the actual
`--state-dir` value as a normalized full path:

- `lib/JevmulatorIdentity.ps1` holds `ConvertFrom-ProcessCommandLine`, which follows the
  `CommandLineToArgvW` rules for quotes and backslashes, `Get-ArgumentValue`, which accepts
  both `--state-dir VALUE` and `--state-dir=VALUE`, `Get-NormalizedDirectory`, and
  `Test-DaemonCommandLine`.
- The module invocation is anchored at argument boundaries: `-m`, `jevmulator` and `serve`
  must be three adjacent arguments. Text inside a path no longer satisfies it.
- A missing or unusable value is a refusal, so the check stays closed.

`jevmulator.ps1` dot-sources that file and **refuses to run without it**, so losing the
library stops the scriptlet rather than silently skipping the proof.

`tests/test_identity.py` adds 33 non-destructive cases that dot-source the same library, so
they exercise the code that runs rather than a copy of it. They cover the reviewer's exact
prefix collision, a nested path, a parent path, a different drive, the expected path inside
another argument, the expected path as the executable, a missing and a dangling
`--state-dir`, four anchoring cases, three fail-closed cases, and six command-line parsing
cases. No case starts or terminates a process.

Running the reviewer's own probe shape against the fixed library:

```
case     : prefix collision
accepted : False
reason   : it owns C:\probe\.jevmulator-other, not C:\probe\.jevmulator

case     : exact match
accepted : True
```

### Final counts

| Suite | After the first review | Now |
|---|---|---|
| `tests/test_http.py` | 65 | 68 |
| `tests/test_identity.py` | — | 33 |
| `tests/test_lifecycle.py` | 34 | 34 |
| Offline selection | 351 passed, 1 skipped | 354 passed, 1 skipped |
| Windows selection | 34 passed | 67 passed, 1 skipped |

```powershell
python -m pytest tests/ -q -m "not windows and not live"
# 354 passed, 1 skipped, 67 deselected in 27.95s

python -m pytest tests/ -q -m "windows"
# 67 passed, 1 skipped, 354 deselected in 272.07s
```

The Windows selection is the 33 identity cases plus the 34 lifecycle cases. The one skip in
each selection is the live module, which declines to run without `JEVMULATOR_LIVE_TESTS=1`.

**433 tests in total: 354 offline, 67 Windows and 12 live.**

The HTTP suite was run three times in a row at 68 passed each time, to confirm the
previously flaky case is stable rather than lucky.

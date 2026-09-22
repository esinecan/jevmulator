# Jevmulator

Jevmulator is a local HTTP daemon. It serves the TypeSafe Jev API surface, so a program
written against Jev can call your own machine instead of `https://api.typesafe.ai`.

The answers come from a model you configure. The default is GLM Flash
(`glm-5.3-flash`) on z.ai. Any OpenAI-compatible Chat Completions endpoint works.

**Jevmulator does not use TypeSafe weights.** It matches the request and response schemas.
It does not match Jev's judgments, its calibration, its confidence numbers or its latency.
Read [Known differences](#known-differences) before you rely on a number it returns.

---

## Contents

- [What it gives you](#what-it-gives-you)
- [Prerequisites](#prerequisites)
- [Install](#install)
- [Quickstart](#quickstart)
- [Start, status and stop](#start-status-and-stop)
- [Calling it](#calling-it)
- [Model discovery](#model-discovery)
- [Configuration reference](#configuration-reference)
- [GLM Flash setup](#glm-flash-setup)
- [Errors](#errors)
- [Logs and diagnostics](#logs-and-diagnostics)
- [Running the tests](#running-the-tests)
- [Troubleshooting](#troubleshooting)
- [Known differences](#known-differences)
- [What is pinned](#what-is-pinned)

---

## What it gives you

Two endpoints, matching the pinned TypeSafe OpenAPI 3.1.0 snapshot:

| Method and path | Purpose |
|---|---|
| `POST /v1/systemone` | Evaluate named questions about one piece of content. |
| `GET /v1/models` | List the model names this daemon accepts. |

Three question types, called primitives:

| Primitive | You supply | You get back |
|---|---|---|
| `noul` | A yes or no statement. | `noul`: the probability of yes, from 0 to 1. There is no confidence field. |
| `choice` | Named options with optional descriptions. | `choice`: the selected option. `probabilities`: the full distribution. `confidence`. |
| `score` | An ordered list of level descriptions, starting at level 0. | `score`: the probability-weighted average level. `legend`: your descriptions. `probabilities`. `confidence`. |

Three operational routes, outside the pinned surface:

| Method and path | Purpose |
|---|---|
| `GET /_jevmulator/health` | Is the daemon up, and can it reach an upstream model? |
| `GET /_jevmulator/status` | The whole configuration and some counters. Never a secret. |
| `GET /_jevmulator/debug/upstream-calls` | The exact payloads sent upstream. Off by default, and requires the bearer token. |

---

## Prerequisites

- **Python 3.11 or later.** Tested on 3.14.0.
- **Windows** for `jevmulator.ps1`. The daemon itself runs anywhere Python runs.
- **An API key for one OpenAI-compatible provider.** The default targets z.ai.
- **Node 20 or later**, only to run the JavaScript conformance tests.

The daemon has **no third-party Python dependencies**. Everything it needs is in the
standard library. Tests need `pytest` and `jsonschema`, which are pinned separately.

---

## Install

Clone the repository, then install it in editable mode with its test dependencies:

```powershell
git clone https://github.com/esinecan/jevmulator.git
cd jevmulator
python -m pip install -e ".[dev]" -c constraints-dev.txt
```

To run the daemon without installing anything, put `src` on the path instead:

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m jevmulator serve
```

`jevmulator.ps1` does this for you.

To build a wheel:

```powershell
python -m pip install build
python -m build
python -m pip install dist\jevmulator-0.1.0-py3-none-any.whl
```

---

## Quickstart

```powershell
# 1. Put your upstream key in the environment. The daemon reads it by name.
$env:ZAI_API_KEY = "<your z.ai key>"

# 2. Choose your own local token. Callers of this daemon send this one, not the z.ai key.
$env:JEVMULATOR_API_KEY = "local-dev-token"

# 3. Start it.
.\jevmulator.ps1 start

# 4. Ask it something.
$request = @{
  model = 'jev-latest'
  state = 'I was charged twice for order 4417. Please refund one.'
  questions = @{
    billing = @{ type = 'noul'; instructions = 'Is this message about billing?' }
  }
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8769/v1/systemone `
  -Headers @{ Authorization = 'Bearer local-dev-token' } `
  -ContentType 'application/json' -Body $request | ConvertTo-Json -Depth 8

# 5. Stop it.
.\jevmulator.ps1 stop
```

> **Do not pass a JSON body inline to `curl.exe` from Windows PowerShell 5.1.** It removes
> the double quotes before the program sees them, and the daemon answers 422
> `json_invalid`. Use `Invoke-RestMethod` as above, or put the body in a file and pass
> `--data "@request.json"`:
>
> ```powershell
> $body = @'
> {"model":"jev-latest","state":"I was charged twice.",
>  "questions":{"billing":{"type":"noul","instructions":"Is this about billing?"}}}
> '@
> [System.IO.File]::WriteAllText("$PWD\request.json", $body, (New-Object System.Text.UTF8Encoding($false)))
> curl.exe -s -X POST http://127.0.0.1:8769/v1/systemone `
>   -H "Authorization: Bearer local-dev-token" `
>   -H "Content-Type: application/json" --data "@request.json"
> ```

The answer looks like this:

```json
{
  "model": "jevmulator-0.1.0-glm-5.3-flash",
  "answers": {
    "billing": { "type": "noul", "noul": 0.97 }
  },
  "usage": { "input_tokens": 142, "output_tokens": 11 }
}
```

`model` names the emulator and the upstream model that answered. It is never a TypeSafe
model name.

---

## Start, status and stop

```powershell
.\jevmulator.ps1 start                 # port 8769
.\jevmulator.ps1 start -Port 8770      # another port
.\jevmulator.ps1 status                # endpoint, process, readiness
.\jevmulator.ps1 status -ShowKey       # also print the daemon API key
.\jevmulator.ps1 stop
.\jevmulator.ps1 restart
```

What each command does:

**`start`** checks whether a daemon is already running, refuses an occupied port before it
launches anything, starts Python in a hidden background process, then polls
`/_jevmulator/health` until the daemon answers. It returns only after the daemon is ready.
It returns a nonzero exit code and a message when it is not.

**`status`** needs no `-Port`. The port is remembered in `.jevmulator/runtime.json`. It
reports the endpoint, the process id and start time, whether the daemon responds, whether
it is ready, the upstream model and the reported model name. The API key stays hidden
unless you add `-ShowKey`.

**`stop`** checks the process id **and** the recorded process start time **and** the
process command line before it terminates anything. A process id that Windows has given to
something else is never killed. A runtime file whose process is gone is removed, and the
command reports that. Running `stop` twice is safe, and so is running it when nothing is
running.

Exit codes:

| Code | Meaning |
|---|---|
| 0 | Success. |
| 1 | Not running, or a live process whose health route does not answer. |
| 2 | The port is already in use. |
| 3 | The daemon exited before it became ready. Read `.jevmulator/daemon.err.log`. |
| 4 | The daemon did not become ready inside `-ReadyTimeoutSeconds`. |
| 6 | The daemon runs but reports itself degraded. Usually a missing upstream key. |

Run the daemon in the foreground instead, when you want to watch it:

```powershell
python -m jevmulator serve --port 8769
```

---

## Calling it

### curl, from a POSIX shell

On Windows PowerShell use `Invoke-RestMethod`, or a request file, as shown in the
[quickstart](#quickstart).

```bash
curl -s -X POST http://127.0.0.1:8769/v1/systemone \
  -H "Authorization: Bearer local-dev-token" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "jev-latest",
    "state": {"subject": "Duplicate charge", "body": "I was billed twice."},
    "questions": {
      "billing": {"type": "noul", "instructions": "Is this about billing?"},
      "tone": {"type": "choice", "instructions": "What is the tone?",
               "criteria": {"angry": "upset", "calm": "neutral", "excited": "eager"}},
      "urgency": {"type": "score", "instructions": "How urgent?",
                  "criteria": ["Can wait", "This week", "Today"]}
    }
  }'
```

### Python, with the official SDK

Install the pinned client:

```powershell
python -m pip install "typesafe-sdk==0.7.1"
```

```python
from typesafe_sdk import TypeSafeClient, Noul, Choice, Score

client = TypeSafeClient(api_key="local-dev-token", base_url="http://127.0.0.1:8769")

response = client.system_one(
    state={"subject": "Duplicate charge", "body": "I was billed twice."},
    questions={
        "billing": Noul(instructions="Is this about billing?"),
        "tone": Choice(
            instructions="What is the tone?",
            criteria={"angry": "upset", "calm": "neutral", "excited": "eager"},
        ),
        "urgency": Score(instructions="How urgent?", criteria=["Can wait", "This week", "Today"]),
    },
)

print(response.model)                       # jevmulator-0.1.0-glm-5.3-flash
print(response.answers["billing"].noul)     # 0.0 to 1.0
print(response.answers["tone"].choice)      # angry, calm or excited
print(response.answers["urgency"].score)    # 0.0 to 2.0
print(response.usage.input_tokens)          # upstream tokens, not TypeSafe tokens
```

Only `base_url` changes. Nothing else in your code has to.

### JavaScript, with the official SDK

Install the pinned client:

```powershell
npm install @typesafe-ai/sdk@0.6.0
```

```javascript
import { TypeSafeClient, noul, choice, score } from '@typesafe-ai/sdk';

const client = new TypeSafeClient({
  apiKey: 'local-dev-token',
  baseURL: 'http://127.0.0.1:8769',
});

const response = await client.systemOne({
  state: { subject: 'Duplicate charge', body: 'I was billed twice.' },
  questions: {
    billing: noul('Is this about billing?'),
    tone: choice('What is the tone?', { angry: 'upset', calm: 'neutral', excited: 'eager' }),
    urgency: score('How urgent?', ['Can wait', 'This week', 'Today']),
  },
});

console.log(response.model);
console.log(response.answers.billing.noul);
console.log(response.answers.tone.choice);
console.log(response.answers.urgency.score);
```

### Structured content

`state`, `instructions` and every criterion description accept a string, an object or an
array. Structure is preserved end to end. A score `legend` comes back holding exactly the
level descriptions you sent, with their objects and arrays intact.

```json
{
  "model": "jev-latest",
  "state": {"ticket": {"body": "refund please", "tags": ["billing"]}},
  "questions": {
    "route": {
      "type": "choice",
      "instructions": {"task": "route this ticket"},
      "criteria": {"billing": {"when": "money is involved"}, "technical": ["errors", "outages"]}
    }
  }
}
```

`instructions` may be omitted or `null`. A choice option may have a `null` description, and
is then read by its name alone.

---

## Model discovery

```powershell
curl.exe -s http://127.0.0.1:8769/v1/models -H "Authorization: Bearer local-dev-token"
```

A GET carries no body, so this form is safe in every shell.

```json
{
  "models": [
    {"name": "jev-latest", "description": "Compatibility alias ... Answered by upstream model glm-5.3-flash via openai, not by TypeSafe weights.", "release_date": "2026-09-22"},
    {"name": "jev-preview", "description": "...", "release_date": "2026-09-22"},
    {"name": "jevmulator-latest", "description": "...", "release_date": "2026-09-22"},
    {"name": "jevmulator-0.1.0-glm-5.3-flash", "description": "...", "release_date": "2026-09-22"}
  ]
}
```

`jev-latest` and `jev-preview` exist so an unmodified client works. Both official SDKs
insert `jev-latest` when the caller names no model.

Every successful response reports `jevmulator-<version>-<upstream model>` in its `model`
field, whichever alias you sent. You can always read which model answered.

An unlisted model name returns 422. Set `JEVMULATOR_UNKNOWN_MODEL=accept` to accept any
name instead.

---

## Configuration reference

Every setting is an environment variable. Copy `.env.example` to `.env` and edit it; the
daemon reads `.env` from the working directory at startup and never overwrites a variable
that is already set.

### Credentials

| Variable | Default | Meaning |
|---|---|---|
| `JEVMULATOR_API_KEY` | generated | The token callers of **this daemon** send. When unset, a random token is generated at startup and written to `.jevmulator/runtime.json`. Read it with `.\jevmulator.ps1 status -ShowKey`. |
| `JEVMULATOR_UPSTREAM_API_KEY_ENV` | `ZAI_API_KEY` | The **name** of the variable holding the upstream key. The key itself stays out of config files. |
| `JEVMULATOR_UPSTREAM_API_KEY` | unset | The upstream key as a literal value. Used only when the variable named above is absent. |

The two credentials never mix. The daemon token is never sent upstream. The upstream key is
sent only in the `Authorization` header of the upstream request.

### Upstream provider

| Variable | Default | Meaning |
|---|---|---|
| `JEVMULATOR_PROVIDER` | `openai` | `openai` for any OpenAI-compatible Chat Completions endpoint. `fake` for the offline deterministic provider. |
| `JEVMULATOR_UPSTREAM_BASE_URL` | `https://api.z.ai/api/coding/paas/v4` | Upstream root. `/chat/completions` is appended. |
| `JEVMULATOR_UPSTREAM_MODEL` | `glm-5.3-flash` | The exact upstream model. It is never substituted. |
| `JEVMULATOR_RESPONSE_FORMAT` | `json_schema` | `json_schema` for strict structured output, `json_object` for JSON mode, `none` for prompted JSON. |
| `JEVMULATOR_UPSTREAM_THINKING` | `disabled` | `disabled` sends `{"thinking": {"type": "disabled"}}`, the z.ai non-thinking mode. `enabled` turns it on. `omit` removes the field for endpoints that reject it. |
| `JEVMULATOR_UPSTREAM_TEMPERATURE` | `0` | Temperature, or `omit` to leave the field out. |
| `JEVMULATOR_MAX_OUTPUT_TOKENS` | `2048` | `max_tokens` on the upstream request. |

### Network

| Variable | Default | Meaning |
|---|---|---|
| `JEVMULATOR_HOST` | `127.0.0.1` | Bind address. Loopback by default. |
| `JEVMULATOR_PORT` | `8769` | TCP port. `-Port` on the scriptlet overrides it. |
| `JEVMULATOR_MAX_BODY_BYTES` | `8388608` | Largest accepted request body. Above it, 413. |

### Timeouts, retries and concurrency

| Variable | Default | Meaning |
|---|---|---|
| `JEVMULATOR_UPSTREAM_TIMEOUT_SECONDS` | `60` | One upstream call. |
| `JEVMULATOR_REQUEST_TIMEOUT_SECONDS` | `120` | The whole evaluation. Past it, outstanding work is cancelled and the daemon returns 504. |
| `JEVMULATOR_INBOUND_TIMEOUT_SECONDS` | `30` | How long a client may take to finish sending its request body. Past it, the daemon returns 408 and closes the connection. |
| `JEVMULATOR_UPSTREAM_RETRIES` | `2` | Retries after a connection error, 408, 429 or 5xx. `retry-after` is honoured. |
| `JEVMULATOR_REPAIR_RETRIES` | `1` | Corrective re-asks after an unusable model answer. |
| `JEVMULATOR_RETRY_BACKOFF_SECONDS` | `0.5` | First backoff step. It doubles each attempt. |
| `JEVMULATOR_RETRY_BACKOFF_MAX_SECONDS` | `8` | Backoff ceiling. |
| `JEVMULATOR_MAX_UPSTREAM_CONCURRENCY` | `4` | Upstream calls running at once. |
| `JEVMULATOR_MAX_INFLIGHT_REQUESTS` | `16` | HTTP requests handled at once. Above it, 429. |

### Compatibility policy

| Variable | Default | Meaning |
|---|---|---|
| `JEVMULATOR_NORMALIZE_PROBABILITIES` | `1` | Rescale a distribution whose sum is off by more than the tolerance. |
| `JEVMULATOR_PROBABILITY_TOLERANCE` | `0.000001` | The tolerance. This is the value the official adapter publishes. |
| `JEVMULATOR_UNKNOWN_MODEL` | `reject` | `reject` returns 422 for an unlisted name. `accept` takes any name. |
| `JEVMULATOR_USAGE_POLICY` | `upstream` | `upstream` reports the counts the provider actually returned. `strict` fails with 502 when the provider reports none. |

### Size guards

| Variable | Default | Meaning |
|---|---|---|
| `JEVMULATOR_MAX_STATE_CHARS` | `0` (off) | Reject a `state` longer than this many characters, with `max_tokens_exceeded`. |
| `JEVMULATOR_MAX_REQUEST_CHARS` | `0` (off) | The same for the whole request. |

These count **characters, not TypeSafe tokens**. TypeSafe's tokenizer is not published, so
the guards are off by default and are an approximation when you turn them on.

### Diagnostics

| Variable | Default | Meaning |
|---|---|---|
| `JEVMULATOR_DEBUG_RECORD` | `0` | `1` exposes `GET /_jevmulator/debug/upstream-calls` holding the exact payloads sent upstream. |
| `JEVMULATOR_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `JEVMULATOR_STATE_DIR` | `.jevmulator` | Where the runtime file and logs live. |

---

## GLM Flash setup

1. Get a key from z.ai.
2. Export it under the name the daemon expects:

   ```powershell
   $env:ZAI_API_KEY = "<your key>"
   ```

   Make it permanent for your account:

   ```powershell
   [Environment]::SetEnvironmentVariable('ZAI_API_KEY', '<your key>', 'User')
   ```

3. Check that the daemon can see it:

   ```powershell
   .\jevmulator.ps1 start
   .\jevmulator.ps1 status
   ```

   `ready : True` means a key is present. `ready : False` with a problem line naming
   `ZAI_API_KEY` means it is not.

The key is referenced by variable name, so `.env` and `/_jevmulator/status` can be read and
shared without exposing it. No code path in this repository logs a key.

To use a different provider, change three variables:

```powershell
$env:JEVMULATOR_UPSTREAM_BASE_URL = "https://api.openai.com/v1"
$env:JEVMULATOR_UPSTREAM_MODEL = "gpt-4o-mini"
$env:JEVMULATOR_UPSTREAM_API_KEY_ENV = "OPENAI_API_KEY"
$env:JEVMULATOR_UPSTREAM_THINKING = "omit"   # the thinking field is a z.ai extension
```

---

## Errors

A 422 carries the TypeSafe validation body: `detail` is an array, and each entry has `loc`,
`msg` and `type`.

```json
{"detail": [{"loc": ["body", "state"], "msg": "Field required", "type": "missing"}]}
```

Every other status carries an object:

```json
{"detail": {"error_type": "upstream_invalid_output", "message": "..."}}
```

| Status | `error_type` | Cause |
|---|---|---|
| 401 | `authentication_error` | Missing or wrong daemon token. |
| 404 | `not_found` | No such route. |
| 405 | `method_not_allowed` | Right route, wrong method. |
| 408 | `request_timeout` | The client declared a body and did not finish sending it inside `JEVMULATOR_INBOUND_TIMEOUT_SECONDS`. |
| 413 | `request_too_large` | Body above `JEVMULATOR_MAX_BODY_BYTES`. |
| 422 | (array body) | The request does not satisfy the pinned schema. |
| 422 | `max_tokens_exceeded` | A character guard rejected the request. |
| 429 | `too_many_requests` | The daemon is already at `JEVMULATOR_MAX_INFLIGHT_REQUESTS`. |
| 429 | `rate_limit_error` | The upstream provider rate limited this daemon. `retry-after` is forwarded when the provider sends it. |
| 502 | `upstream_invalid_output` | The model's answer could not be used, after the repair retries were spent. |
| 502 | `upstream_refusal` | The model declined to answer. |
| 502 | `upstream_error` | The provider failed in another way. |
| 502 | `upstream_not_configured` | No upstream key is available. |
| 502 | `usage_unavailable` | `JEVMULATOR_USAGE_POLICY=strict` and the provider reported no token counts. |
| 504 | `upstream_timeout` | A call or the whole request passed its deadline. |
| 529 | `overloaded` | The provider reported 503 or 529. |

**A failure is never an answer.** When a question cannot be answered, the whole request
fails with one of the statuses above. The daemon does not fill in a plausible distribution,
and it does not return a partial `answers` object.

---

## Logs and diagnostics

Started through the scriptlet, the daemon writes to:

- `.jevmulator/daemon.out.log`
- `.jevmulator/daemon.err.log`
- `.jevmulator/runtime.json` — the process id, port, base URL and the generated API key.

The whole `.jevmulator/` directory is excluded from git.

Read the configuration and the counters at any time:

```bash
curl -s http://127.0.0.1:8769/_jevmulator/status
```

It reports `upstream_api_key_env` and `upstream_api_key_present`. It never reports a key.

To see exactly what goes upstream, turn recording on:

```powershell
$env:JEVMULATOR_DEBUG_RECORD = "1"
.\jevmulator.ps1 restart
curl.exe -s http://127.0.0.1:8769/_jevmulator/debug/upstream-calls `
  -H "Authorization: Bearer local-dev-token"
```

**This route requires the bearer token.** The recording holds your state, your instructions
and the whole prompts, so it is at least as sensitive as the evaluate route. It holds the
last 200 calls, and it returns 404 to an authenticated caller when recording is off.

`/_jevmulator/health` and `/_jevmulator/status` need no token. Readiness polling has to work
before a caller holds a key, and neither route returns a secret or any caller content.

---

## Running the tests

### Offline

```powershell
python -m pip install -e ".[dev]" -c constraints-dev.txt
python -m pytest tests/ -q -m "not live"
```

No test in that selection reaches a provider. The default provider inside tests is the
deterministic fake, and the failure cases speak to a fake upstream HTTP server bound to
loopback.

### The JavaScript leg

```powershell
cd tests\js
npm ci
cd ..\..
python -m pytest tests/test_sdk_js.py -q
```

### Windows lifecycle

```powershell
python -m pytest tests/test_lifecycle.py -q
```

Each case starts a real daemon through `jevmulator.ps1` in a temporary directory whose path
contains a space.

### Live GLM Flash

These cost money and reach z.ai. They are skipped unless you opt in:

```powershell
$env:ZAI_API_KEY = "<your key>"
$env:JEVMULATOR_LIVE_TESTS = "1"
python -m pytest tests/live -q -s
```

The module stops at 30 upstream calls. Raise or lower that with
`JEVMULATOR_LIVE_CALL_CEILING`. Timings, token counts and judgment verdicts are written to
`live-evidence/glm-flash-run.json`, which git ignores.

Full results from the run in this repository are in [docs/testing.md](docs/testing.md).

---

## Troubleshooting

**`.\jevmulator.ps1 : File cannot be loaded because running scripts is disabled`**
Run it with an explicit policy for that process:
`powershell -ExecutionPolicy Bypass -File .\jevmulator.ps1 start`.

**`port 8769 is already in use`**
Something else holds the port. Pick another with `-Port 8770`, or find the holder with
`netstat -ano | findstr :8769`.

**`the daemon exited with code ... before it was ready`**
Read `.jevmulator\daemon.err.log`. The usual cause is that Python cannot import
`jevmulator`. Install the package, or let the scriptlet set `PYTHONPATH` by running it from
the repository root.

**`status` says `ready : False`**
The daemon is up but has no upstream key. The problem line names the variable it looked for.
Set that variable and restart.

**`401 Invalid or missing API key`**
You sent the wrong daemon token. Print the current one with
`.\jevmulator.ps1 status -ShowKey`, or set `JEVMULATOR_API_KEY` yourself and restart.

**`422 Unknown model`**
The name is not in `GET /v1/models`. Use `jev-latest`, or set
`JEVMULATOR_UNKNOWN_MODEL=accept`.

**`422 json_invalid` from a PowerShell `curl.exe` command**
Windows PowerShell 5.1 removes the double quotes from an inline JSON argument before
`curl.exe` sees it. Use `Invoke-RestMethod`, or write the body to a file and pass
`--data "@request.json"`. Both forms are in the [quickstart](#quickstart).

**`502 upstream_invalid_output`**
The model did not return a usable distribution, even after a corrective re-ask. Raise
`JEVMULATOR_REPAIR_RETRIES`, or switch `JEVMULATOR_RESPONSE_FORMAT` to `json_schema` if you
moved it off the default.

**`504 upstream_timeout` on a large batch**
Every question is one upstream call, and four run at a time. Raise
`JEVMULATOR_REQUEST_TIMEOUT_SECONDS`, or raise `JEVMULATOR_MAX_UPSTREAM_CONCURRENCY`.

**`stop` or `status` says "Identity check refused"**
The scriptlet acts on a process only when it can prove the process is this checkout's
daemon: the recorded creation time must match, and the command line must invoke
`-m jevmulator serve` and name this checkout's `.jevmulator` directory. When any proof is
missing it terminates nothing and prints the reason. Common reasons:

- *records no creation time* — the daemon was started by hand with
  `python -m jevmulator serve` rather than by the scriptlet. Stop it with Ctrl+C.
- *is a different process* — Windows gave that process id to an unrelated program. Nothing
  was terminated and the runtime file was removed. Start again.
- *belongs to a different Jevmulator checkout* — another clone owns that daemon. Stop it
  from its own directory.

---

## Known differences

Schema parity is not calibration parity. These are the boundaries.

### The judgments come from GLM Flash

A Jevmulator number tells you what `glm-5.3-flash` thinks. A Jev number tells you what
Jev thinks. They are different models. Probabilities, confidence values and scores will
differ, sometimes a great deal. Do not port a threshold tuned on Jev to Jevmulator without
retuning it.

### Confidence is a documented policy, not Jev's formula

Confidence is computed with the two algorithms published in TypeSafe's own
`system-one-adapter-python`, at commit `e1d4cc938204b22fc5a3c3aca7044072fe3f712d`:

- choice: `(max(p) - 1/N) / (1 - 1/N)`
- score: `max(0, 1 - D/U)`, around the first modal level

Those are verified adapter algorithms. They are **not** verified Jev production algorithms.
TypeSafe's own confidence documentation calls its worked example an approximation, and some
published example numbers do not equal these formulas.

### Token counts are upstream tokens

`usage.input_tokens` and `usage.output_tokens` are the sums of the counts the upstream
provider reported, across one call per question. They are not TypeSafe tokens and not
TypeSafe billing. The two tokenizers are different, and a Jevmulator request makes several
upstream calls where Jev makes one.

When a provider reports no counts, the response header `X-Jevmulator-Usage-Source` reads
`upstream-partial`. No count is ever invented. Set `JEVMULATOR_USAGE_POLICY=strict` to fail
the request instead.

### Latency is different

Jevmulator issues one upstream call per question, up to four at a time. A ten-question
request is three rounds of upstream latency. Jev's own latency is a different figure and is
not reproduced here.

### The choice tie rule is ours

When two options share the highest probability, Jevmulator picks the one that appeared
first in your `criteria`. TypeSafe does not document its rule.

### Rounding is not applied

Jevmulator emits full floating-point precision. TypeSafe's precision and rounding mode are
not published, so no rounding is imitated.

### Question isolation is enforced here, and differs from the official adapter

The pinned contract says questions are independent and question IDs are not model input.
Jevmulator enforces both: one upstream call per question, carrying only the state, that
question's instructions and that question's criteria, under the fixed answer key `answer`.

TypeSafe's own adapter does the opposite. It builds one output model whose fields are named
after your question IDs and sends one call for all questions. If you compare the two, expect
different behaviour here, by design.

### Error bodies are ours

The 422 body matches the published `HTTPValidationError` schema. Every other error body is
Jevmulator's own `{"detail": {"error_type": ..., "message": ...}}`. TypeSafe's exact 401,
429 and 529 bodies are not published.

### Client narrowings worth knowing

- The retained `jev-review` client accepts only **string** score legend values. The pinned
  OpenAPI permits objects and arrays. Send string levels to that client, or widen it.
- The Python SDK turns score map keys into integers. The HTTP JSON keys stay strings.
- The Python SDK tolerates missing or null usage. The wire schema requires integers, and
  Jevmulator always sends integers.

### Still unverified in TypeSafe itself

1. Production confidence formula, numeric precision, rounding mode and tie rule.
2. Runtime acceptance of a one-level score, a zero-option choice and empty content. The
   schema permits all three, and Jevmulator accepts them.
3. Exact 401, 422, 429 and 529 bodies and headers, and unknown-model behaviour.
4. The tokenizer and the billing counts.

---

## What is pinned

| Thing | Pin |
|---|---|
| TypeSafe OpenAPI snapshot | 3.1.0, `info.version` 0.2.0, SHA-256 `a191f8a7df6bd6fedced8120dd0fd106f88575d1d1c8360d08900a6c7c0360d5`, retrieved 2026-09-22 |
| Official Python SDK | `typesafe-sdk==0.7.1`, commit `0ffd094c72ed9445223060b24ffd7a56aa781fb4` |
| Official JavaScript SDK | `@typesafe-ai/sdk@0.6.0`, commit `66880ccded6cb642dc1809620c2b108c33730214` |
| Official adapter, read but not vendored | `system-one-adapter-python`, commit `e1d4cc938204b22fc5a3c3aca7044072fe3f712d` |
| Default upstream | `glm-5.3-flash` at `https://api.z.ai/api/coding/paas/v4` |

The snapshot lives in `contract/`. `tests/test_schema_conformance.py` re-hashes it on every
run, so a silent edit fails the suite.

Jevmulator does not follow TypeSafe's documentation as it changes, and it does not follow
`jev-latest` automatically. Updating means taking a new dated snapshot, diffing the schema
and the semantic assertions, then re-running the conformance and SDK suites.

Provenance of every third-party file is recorded in [NOTICES.md](NOTICES.md). The design and
its reasoning are in [docs/design.md](docs/design.md). Test commands and results are in
[docs/testing.md](docs/testing.md).

---

## Licence

Copyright (c) 2026 Eren Sinecan. All rights reserved. See [LICENSE](LICENSE).

This project is an independent emulator of a published HTTP contract. It is not affiliated
with, endorsed by, or connected to TypeSafe, and it does not use TypeSafe model weights.

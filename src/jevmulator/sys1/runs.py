"""sys1 runs: one agent run per request, decided by one state machine.

A run starts RUNNING and ends in exactly one terminal state. The first transition wins,
and a form submission is validated and applied under the same lock, so exactly one
outcome is ever decided. A supervisor thread owns each run: it prepares the run
directory, launches the harness, watches the event file and the process, and closes the
run's job. Request handlers only wait for the published outcome.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import json
import logging
import math
import os
import re
import shutil
import stat
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import __version__, errors, wire
from ..evaluator import UsageTally
from . import brief as brief_module
from . import form
from .harness.base import Harness, HarnessProcess, LaunchSpec
from .harness.events import EventTail, digest
from .profiles import Profile

LOGGER = logging.getLogger("jevmulator.sys1")

RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
TICK_SECONDS = 0.25
OBSERVE_EVERY_SECONDS = 1.0
#: After the harness exits, a submission it already sent may still be in a handler.
EXIT_SUBMISSION_GRACE_SECONDS = 2.0
#: A handler waits this much beyond the run deadline and the exit grace.
HANDLER_MARGIN_SECONDS = 10.0
CACHE_CAP = 256
LIVE_RUNS_CAP = 256
MAX_FAILURE_NOTES = 20


class RunState(str, enum.Enum):
    RUNNING = "RUNNING"
    ACCEPTED = "ACCEPTED"
    EXHAUSTED = "EXHAUSTED"
    NO_VERDICT = "NO_VERDICT"
    TIMED_OUT = "TIMED_OUT"
    STALLED = "STALLED"
    MISMATCH = "MISMATCH"
    CANCELLED = "CANCELLED"
    FAILED_TO_START = "FAILED_TO_START"


ERROR_FOR_STATE: dict[RunState, type[errors.JevmulatorError]] = {
    RunState.EXHAUSTED: errors.Sys1InvalidSubmissionError,
    RunState.NO_VERDICT: errors.Sys1NoVerdictError,
    RunState.TIMED_OUT: errors.Sys1TimeoutError,
    RunState.STALLED: errors.Sys1StalledError,
    RunState.MISMATCH: errors.Sys1HarnessMismatchError,
    RunState.CANCELLED: errors.Sys1ShuttingDownError,
    RunState.FAILED_TO_START: errors.Sys1HarnessNotConfiguredError,
}

#: Outcomes that say nothing about the request itself, so replaying them would mislead.
UNCACHED_STATES = frozenset({RunState.CANCELLED, RunState.FAILED_TO_START})


@dataclass
class Outcome:
    """What every handler waiting on one run sends back."""

    status: int
    body: dict[str, Any]
    headers: dict[str, str]
    state: RunState


@dataclass(frozen=True)
class RunSettings:
    home: Path
    max_submissions: int
    max_sum_error: float
    normalize: bool
    tolerance: float
    usage_policy: str
    run_timeout_seconds: float
    stall_seconds: float
    hello_seconds: float
    exit_grace_seconds: float
    max_nudges: int


def fingerprint(profile_name: str, payload: dict[str, Any]) -> str:
    """Identify a request for coalescing.

    Key order is kept, not sorted: the order of choice labels decides ties and the key
    order of the response, so reordered labels are a different request. The raw model
    string is left out, so every alias of one profile coalesces.
    """
    material = json.dumps(
        {"profile": profile_name, "state": payload.get("state"), "questions": payload.get("questions")},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


#: Windows refuses to replace a file that another process holds open, even only for
#: reading, with WinError 5. A reader holds a record for milliseconds, so a short retry
#: outlasts it.
REPLACE_ATTEMPTS = 40
REPLACE_PAUSE_SECONDS = 0.05


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    for _attempt in range(REPLACE_ATTEMPTS - 1):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            time.sleep(REPLACE_PAUSE_SECONDS)
    os.replace(temporary, path)


class Run:
    """One agent run, from launch to its single outcome."""

    def __init__(
        self,
        *,
        run_id: str,
        token: str,
        fingerprint: str,
        profile: Profile,
        request: wire.SystemOneRequest,
        aliased: form.AliasedRequest,
        harness: Harness,
        settings: RunSettings,
        base_url: str,
    ) -> None:
        self.run_id = run_id
        self.token = token
        self.fingerprint = fingerprint
        self.profile = profile
        self.request = request
        self.aliased = aliased
        self.harness = harness
        self.settings = settings
        self.base_url = base_url

        self.lock = threading.Lock()
        self.state = RunState.RUNNING
        self.reason = ""
        self.changed = threading.Event()
        self.done = threading.Event()
        self.outcome: Outcome | None = None

        self.created_at = time.time()
        self.started = time.monotonic()
        self.timeout_seconds = min(profile.timeout_seconds, settings.run_timeout_seconds)
        self.deadline = self.started + self.timeout_seconds
        self.attempts_left = settings.max_submissions
        self.submissions: list[dict[str, Any]] = []
        self.accepted: form.FormResult | None = None
        self.accepted_raw: Any = None
        self.hello_report: dict[str, Any] | None = None
        self.hello_at: float | None = None
        self.attachments: list[dict[str, Any]] = []
        self.failure_notes: list[str] = []
        self.usage = UsageTally()
        self.models_seen: set[str] = set()
        self.process: HarnessProcess | None = None
        self.active_after_close: int | None = None
        self.finished_at: float | None = None
        self._tail: EventTail | None = None
        self._on_decided: Callable[["Run"], None] | None = None
        #: Set at shutdown, so a decided run stops waiting for its harness to exit.
        self.stop_waiting = threading.Event()

        self.run_dir = settings.home / "runs" / run_id
        self.work_dir = self.run_dir / "work" if profile.workdir == "scratch" else Path(profile.workdir)
        self.expected_tools = harness.tool_names(profile)
        self.expected_model = harness.model_id(profile)
        self.model_name = (
            f"jevmulator-{__version__}-sys1-{profile.name}-{harness.name}-"
            + self.expected_model.split("/", 1)[-1]
        )

    # -- transitions -----------------------------------------------------------

    def _transition_locked(self, state: RunState, reason: str) -> bool:
        if self.state is not RunState.RUNNING:
            return False
        self.state = state
        self.reason = reason
        self.changed.set()
        return True

    def transition(self, state: RunState, reason: str) -> bool:
        with self.lock:
            return self._transition_locked(state, reason)

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def handler_wait_seconds(self) -> float:
        return self.remaining_seconds() + self.settings.exit_grace_seconds + HANDLER_MARGIN_SECONDS

    # -- what the harness calls ----------------------------------------------------

    def check_token(self, token: str) -> bool:
        return hmac.compare_digest(token.encode("utf-8"), self.token.encode("utf-8"))

    def _closed_error(self) -> errors.Sys1RunClosedError:
        return errors.Sys1RunClosedError(
            f"Run {self.run_id} is {self.state.value}. It accepts no more posts."
        )

    def submit(self, payload: Any) -> dict[str, Any]:
        with self.lock:
            if self.state is not RunState.RUNNING:
                raise self._closed_error()
            result = form.validate_submission(
                payload,
                self.aliased,
                normalize=self.settings.normalize,
                tolerance=self.settings.tolerance,
                max_sum_error=self.settings.max_sum_error,
            )
            self.submissions.append(
                {"at": _iso(time.time()), "accepted": result.accepted, "problems": result.problems}
            )
            if result.accepted:
                self.accepted = result
                self.accepted_raw = payload.get("answers")
                self._transition_locked(RunState.ACCEPTED, "a submission was accepted")
                return {"accepted": True, "message": "Accepted. Your verdict is recorded. Stop now."}
            self.attempts_left -= 1
            if self.attempts_left <= 0:
                self._transition_locked(
                    RunState.EXHAUSTED,
                    f"all {self.settings.max_submissions} submissions were rejected",
                )
                return {
                    "accepted": False,
                    "problems": result.problems,
                    "attempts_left": 0,
                    "message": "No attempts are left. Stop now.",
                }
            return {
                "accepted": False,
                "problems": result.problems,
                "attempts_left": self.attempts_left,
            }

    def hello(self, payload: Any) -> dict[str, Any]:
        with self.lock:
            if self.state is not RunState.RUNNING:
                raise self._closed_error()
            tools = payload.get("tools") if isinstance(payload, dict) else None
            model = payload.get("model") if isinstance(payload, dict) else None
            selected = payload.get("selected_tools") if isinstance(payload, dict) else None
            self.hello_report = {"tools": tools, "model": model, "selected_tools": selected}
            self.hello_at = time.monotonic()
            problems = []
            if not isinstance(tools, list) or sorted(str(tool) for tool in tools) != self.expected_tools:
                problems.append(
                    f"the harness offered the tools {tools}; the profile grants {self.expected_tools}"
                )
            if model != self.expected_model:
                problems.append(f"the harness runs {model}; the profile names {self.expected_model}")
            if problems:
                self._transition_locked(RunState.MISMATCH, "; ".join(problems))
                return {
                    "ok": False,
                    "problems": problems,
                    "message": "The harness does not match the profile. Stop now.",
                }
            return {"ok": True}

    def note_attachment(self, kind: str, retry_count: str | None) -> None:
        with self.lock:
            self.attachments.append({"at": _iso(time.time()), "kind": kind, "retry_count": retry_count})

    # -- supervision -----------------------------------------------------------------

    def supervise(
        self,
        on_decided: Callable[["Run"], None],
        on_finished: Callable[["Run"], None],
    ) -> None:
        """Run the agent to one outcome.

        ``on_decided`` is called once the outcome is published: the run's slot is free and
        its outcome can be replayed from then on. ``on_finished`` is called after the job
        is closed and the final record is written.
        """
        self._on_decided = on_decided
        try:
            try:
                self._prepare()
                self.process = self.harness.launch(self._launch_spec())
                # The record names the run's processes from the start, so a daemon that is
                # killed mid-run still leaves a record anyone can check them against.
                self.write_record()
            except Exception as exc:  # noqa: BLE001 - any launch failure ends the run
                LOGGER.warning("sys1 run %s could not start: %s", self.run_id, exc)
                self.transition(RunState.FAILED_TO_START, f"the harness could not start: {exc}")
            if self.process is not None:
                self._watch()
            self._finish()
        except Exception:  # noqa: BLE001 - a supervisor fault must still answer handlers
            LOGGER.exception("sys1 run %s failed inside its supervisor", self.run_id)
            self.transition(RunState.NO_VERDICT, "the run's supervisor failed; see the daemon log")
            self._close_job()
            if not self.done.is_set():
                self._publish(self._error_outcome())
        finally:
            on_finished(self)

    def _launch_spec(self) -> LaunchSpec:
        return LaunchSpec(
            run_id=self.run_id,
            run_dir=self.run_dir,
            work_dir=self.work_dir,
            session_dir=self.run_dir / "session",
            brief_path=self.run_dir / "brief.md",
            events_path=self.run_dir / "events.jsonl",
            stderr_path=self.run_dir / "stderr.log",
            tools=self.profile.tools,
            read_roots=self.profile.read_roots,
            settings=self.profile.harness_settings(self.harness.name),
            submit_url=f"{self.base_url}/_jevmulator/sys1/runs/{self.run_id}/submission",
            hello_url=f"{self.base_url}/_jevmulator/sys1/runs/{self.run_id}/hello",
            token=self.token,
            max_nudges=self.settings.max_nudges,
        )

    def _prepare(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=False)
        (self.run_dir / "session").mkdir()
        if self.profile.workdir == "scratch":
            self.work_dir.mkdir()
        state_path = self.run_dir / "state.json"
        _write_json(state_path, self.request.state)
        _write_json(self.run_dir / "questions.json", form.questions_document(self.aliased))
        _write_json(self.run_dir / "submission.schema.json", form.submission_schema(self.aliased))
        text = brief_module.render_brief(
            self.profile,
            self.aliased,
            self.request.state,
            state_path=str(state_path),
            workdir=str(self.work_dir),
            tool_names=[name for name in self.expected_tools if name != "submit_verdict"],
            max_submissions=self.settings.max_submissions,
            max_sum_error=self.settings.max_sum_error,
        )
        (self.run_dir / "brief.md").write_text(text, encoding="utf-8", newline="\n")
        self.write_record()

    def _absorb(self, event: dict[str, Any]) -> None:
        info = digest(event)
        if info.is_model_call:
            self.usage.calls += 1
            if info.input_tokens is None or info.output_tokens is None:
                self.usage.calls_without_usage += 1
            else:
                self.usage.input_tokens += info.input_tokens
                self.usage.output_tokens += info.output_tokens
            if info.model:
                self.models_seen.add(info.model)
                if info.model != self.expected_model:
                    self.transition(
                        RunState.MISMATCH,
                        f"a model call used {info.model}; the profile names {self.expected_model}",
                    )
        if info.failure and len(self.failure_notes) < MAX_FAILURE_NOTES:
            self.failure_notes.append(info.failure)

    def _watch(self) -> None:
        assert self.process is not None
        self._tail = EventTail(self.run_dir / "events.jsonl")
        last_event = time.monotonic()
        last_observe = 0.0
        exit_seen: float | None = None
        while True:
            self.changed.wait(TICK_SECONDS)
            now = time.monotonic()
            for event in self._tail.read_new():
                last_event = now
                self._absorb(event)
            if now - last_observe >= OBSERVE_EVERY_SECONDS:
                known = len(self.process.job.observed())
                self.process.job.observe()
                last_observe = now
                if len(self.process.job.observed()) != known:
                    self.write_record()
            if self.state is not RunState.RUNNING:
                return
            if self.hello_at is None and now - self.started >= self.settings.hello_seconds:
                self.transition(
                    RunState.MISMATCH,
                    f"the harness reported no tools and model within {self.settings.hello_seconds} seconds",
                )
            elif now >= self.deadline:
                self.transition(
                    RunState.TIMED_OUT, f"no verdict was accepted within {self.timeout_seconds} seconds"
                )
            elif now - last_event >= self.settings.stall_seconds:
                self.transition(
                    RunState.STALLED,
                    f"the harness produced no event for {self.settings.stall_seconds} seconds",
                )
            else:
                code = self.process.poll()
                if code is not None:
                    if exit_seen is None:
                        exit_seen = now
                    elif now - exit_seen >= EXIT_SUBMISSION_GRACE_SECONDS:
                        self.transition(
                            RunState.NO_VERDICT,
                            f"the harness exited with code {code} without an accepted submission",
                        )

    def _finish(self) -> None:
        if self.state is RunState.ACCEPTED:
            if self._tail is not None:
                for event in self._tail.read_new():
                    self._absorb(event)
            self._publish(self._success_outcome())
            self._wait_for_exit(self.settings.exit_grace_seconds)
            self._close_job()
        else:
            self._close_job()
            if self._tail is not None:
                for event in self._tail.read_new(final=True):
                    self._absorb(event)
            self._publish(self._error_outcome())
        self.finished_at = time.time()
        self.write_record(final=True)

    def _wait_for_exit(self, grace: float) -> None:
        if self.process is None:
            return
        deadline = time.monotonic() + grace
        while self.process.poll() is None and time.monotonic() < deadline:
            if self.stop_waiting.wait(0.1):
                return

    def _close_job(self) -> None:
        if self.process is None or self.active_after_close is not None:
            return
        try:
            self.active_after_close = self.process.job.terminate()
        finally:
            self.process.job.close()

    def _publish(self, outcome: Outcome) -> None:
        with self.lock:
            if self.outcome is not None:
                return
            self.outcome = outcome
        self.done.set()
        if self._on_decided is not None:
            self._on_decided(self)

    def _success_outcome(self) -> Outcome:
        assert self.accepted is not None and self.accepted.answers is not None
        complete = self.usage.calls > 0 and self.usage.calls_without_usage == 0
        if self.settings.usage_policy == "strict" and not complete:
            error = errors.UsageUnavailableError(
                "The harness reported no token count for at least one model call. "
                "JEVMULATOR_USAGE_POLICY=strict refuses to emit an unbacked count."
            )
            return Outcome(error.status, error.body(), dict(error.headers), RunState.ACCEPTED)
        answers = {
            self.aliased.id_for(alias): self.accepted.answers[alias] for alias in self.aliased.aliases
        }
        body = wire.system_one_response(
            self.model_name, answers, self.usage.input_tokens, self.usage.output_tokens
        )
        headers = {
            "X-Jevmulator-Sys1-Profile": self.profile.name,
            "X-Jevmulator-Harness": self.harness.name,
            "X-Jevmulator-Usage-Source": "upstream" if complete else "upstream-partial",
        }
        return Outcome(200, body, headers, RunState.ACCEPTED)

    def _error_outcome(self) -> Outcome:
        error_class = ERROR_FOR_STATE.get(self.state, errors.Sys1NoVerdictError)
        message = f"sys1 run {self.run_id}: {self.reason or 'no outcome was reached'}."
        if self.failure_notes:
            message += " Last harness failure: " + self.failure_notes[-1]
        error = error_class(message)
        headers = dict(error.headers)
        headers["X-Jevmulator-Sys1-Profile"] = self.profile.name
        headers["X-Jevmulator-Harness"] = self.harness.name
        return Outcome(error.status, error.body(), headers, self.state)

    # -- the record ---------------------------------------------------------------------

    def write_record(self, *, final: bool = False) -> None:
        record: dict[str, Any] = {
            "run_id": self.run_id,
            "status": self.state.value,
            "reason": self.reason,
            "profile": self.profile.name,
            "harness": self.harness.name,
            "model": self.expected_model,
            "model_name": self.model_name,
            "fingerprint": self.fingerprint,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at) if self.finished_at else None,
            "timeout_seconds": self.timeout_seconds,
            "expected_tools": self.expected_tools,
            "hello": self.hello_report,
            "submissions": self.submissions,
            "attempts_left": self.attempts_left,
            "accepted": (
                {
                    "rationale": self.accepted.rationale,
                    "evidence": self.accepted.evidence,
                    # The distributions exactly as the agent submitted them, so anyone can
                    # recompute the derived fields in the response by hand.
                    "submitted_answers": self.accepted_raw,
                }
                if self.accepted is not None
                else None
            ),
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "model_calls": self.usage.calls,
                "model_calls_without_usage": self.usage.calls_without_usage,
            },
            "models_seen": sorted(self.models_seen),
            "failure_notes": self.failure_notes,
            "attachments": self.attachments,
            "containment": self.process.job.containment if self.process else None,
            "processes": self.process.job.observed() if self.process else [],
            "active_processes_after_close": self.active_after_close,
            "outcome_status": self.outcome.status if self.outcome else None,
            "paths": {
                "brief": str(self.run_dir / "brief.md"),
                "events": str(self.run_dir / "events.jsonl"),
                "stderr": str(self.run_dir / "stderr.log"),
                "session": str(self.run_dir / "session"),
                "work": str(self.work_dir),
            },
        }
        if final:
            # Written only after the job is closed, so no agent process can read them.
            record["question_ids"] = dict(zip(self.aliased.aliases, self.aliased.question_ids))
        if not self.run_dir.is_dir():
            return
        try:
            _write_json(self.run_dir / "record.json", record)
        except OSError as exc:
            # The outcome is already decided and published. A record that cannot be
            # written is logged; it never changes what the caller receives.
            LOGGER.warning("sys1 run %s: the record could not be written: %s", self.run_id, exc)


class Registry:
    """Which runs are live, and which recent outcomes a retry may reuse."""

    def __init__(
        self,
        *,
        max_runs: int,
        result_ttl_seconds: float,
        failure_ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_runs = max_runs
        self._result_ttl = result_ttl_seconds
        self._failure_ttl = failure_ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._inflight: dict[str, Run] = {}
        self._cache: OrderedDict[str, tuple[float, Outcome, str]] = OrderedDict()
        self._live: OrderedDict[str, Run] = OrderedDict()
        self._closing = False

    def begin(self, key: str, factory: Callable[[], Run]) -> tuple[str, Run | None, Outcome | None, str]:
        """Attach to, replay, or start the run for ``key``.

        Returns the coalescing kind (``new``, ``attached`` or ``replayed``), the run when
        one is live, the replayed outcome, and the run id.
        """
        with self._lock:
            now = self._clock()
            for stale in [k for k, (expires, _o, _r) in self._cache.items() if expires <= now]:
                del self._cache[stale]
            if self._closing:
                raise errors.Sys1ShuttingDownError("The daemon is shutting down.")
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return "replayed", None, cached[1], cached[2]
            run = self._inflight.get(key)
            if run is not None:
                if run.done.is_set() and run.outcome is not None:
                    return "replayed", None, run.outcome, run.run_id
                return "attached", run, None, run.run_id
            if len(self._inflight) >= self._max_runs:
                busiest = max(run.remaining_seconds() for run in self._inflight.values())
                retry_after = max(1, math.ceil(busiest))
                raise errors.Sys1BusyError(
                    f"All {self._max_runs} sys1 run slots are busy. The busiest run has "
                    f"{retry_after} seconds left.",
                    headers={"Retry-After": str(retry_after)},
                )
            run = factory()
            self._inflight[key] = run
            self._live[run.run_id] = run
            while len(self._live) > LIVE_RUNS_CAP:
                oldest = next(iter(self._live))
                if self._live[oldest].done.is_set():
                    del self._live[oldest]
                else:
                    break
            return "new", run, None, run.run_id

    def decided(self, run: Run) -> None:
        """A run published its outcome: free its slot, and keep the outcome for replay.

        The run's harness may still be exiting. Its slot is free anyway: a caller that sends
        the next request right after a verdict must not wait for another program's cleanup.
        """
        with self._lock:
            if self._inflight.get(run.fingerprint) is run:
                del self._inflight[run.fingerprint]
            outcome = run.outcome
            if outcome is None or self._closing or outcome.state in UNCACHED_STATES:
                return
            ttl = self._result_ttl if outcome.status == 200 else self._failure_ttl
            if ttl <= 0:
                return
            self._cache[run.fingerprint] = (self._clock() + ttl, outcome, run.run_id)
            self._cache.move_to_end(run.fingerprint)
            while len(self._cache) > CACHE_CAP:
                self._cache.popitem(last=False)

    def find(self, run_id: str) -> Run | None:
        with self._lock:
            return self._live.get(run_id)

    def inflight(self) -> list[Run]:
        with self._lock:
            return list(self._inflight.values())

    def close(self) -> list[Run]:
        with self._lock:
            self._closing = True
            return list(self._inflight.values())


def _clear_readonly_onerror(function, path, _info) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _clear_readonly_onexc(function, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove_tree(path: Path) -> None:
    """Remove a run directory, clearing read-only bits an agent may have set."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_clear_readonly_onexc)
    else:  # pragma: no cover - Python 3.11
        shutil.rmtree(path, onerror=_clear_readonly_onerror)


def reconcile(runs_root: Path) -> int:
    """Mark records a previous daemon left RUNNING as ABANDONED. Returns how many."""
    count = 0
    if not runs_root.is_dir():
        return 0
    for record_path in runs_root.glob("*/record.json"):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("status") == RunState.RUNNING.value:
            record["status"] = "ABANDONED"
            record["reason"] = "the daemon stopped before this run reached an outcome"
            try:
                _write_json(record_path, record)
                count += 1
            except OSError:
                continue
    return count


def prune(runs_root: Path, keep: int, live: set[str]) -> int:
    """Keep the newest ``keep`` run directories, never touching a live run."""
    if not runs_root.is_dir():
        return 0
    directories = [
        entry for entry in runs_root.iterdir()
        if entry.is_dir() and RUN_ID_PATTERN.match(entry.name) and entry.name not in live
    ]
    directories.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
    removed = 0
    for entry in directories[keep:]:
        try:
            remove_tree(entry)
            removed += 1
        except OSError as exc:
            LOGGER.warning("could not prune sys1 run %s: %s", entry.name, exc)
    return removed

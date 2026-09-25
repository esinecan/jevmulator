"""sys1 over HTTP, on the scripted fake harness.

Every run here is a real child process in a real job, speaking the real protocol to a
real daemon. Only the agent's judgment is scripted.
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import pytest

from conftest import start_daemon

MIXED = {
    "model": "jev-latest",
    "state": "I was charged twice for order 4417. Please refund one.",
    "questions": {
        "billing": {"type": "noul", "instructions": "Is this message about billing?"},
        "tone": {"type": "choice", "criteria": {"angry": "upset", "calm": None, "excited": "eager"}},
        "urgency": {"type": "score", "criteria": ["Can wait", "This week", "Today"]},
    },
}


# -- helpers ------------------------------------------------------------------------------


def write_script(tmp_path: Path, steps: list[dict[str, Any]] | None, hello: str) -> str:
    path = tmp_path / f"script-{uuid.uuid4().hex[:8]}.json"
    content: dict[str, Any] = {"hello": hello}
    if steps is not None:
        content["steps"] = steps
    path.write_text(json.dumps(content), encoding="utf-8")
    return str(path)


@contextmanager
def daemon(tmp_path: Path, steps: list[dict[str, Any]] | None = None, *, hello: str = "correct", **env: str):
    env.setdefault("JEVMULATOR_SYS1_HOME", str(tmp_path / "home"))
    env.setdefault("JEVMULATOR_SYS1_EXIT_GRACE_SECONDS", "2")
    if steps is not None or hello != "correct":
        env["JEVMULATOR_SYS1_FAKE_SCRIPT"] = write_script(tmp_path, steps, hello)
    with start_daemon(**env) as running:
        running.home = Path(env["JEVMULATOR_SYS1_HOME"])
        yield running


def sys1(running, body: Any = MIXED, **kwargs):
    kwargs.setdefault("timeout", 60)
    return running.client.request("POST", "/sys1/v1/systemone", body, **kwargs)


def wait_for(predicate: Callable[[], Any], timeout: float = 15.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def run_dirs(running) -> list[Path]:
    root = running.home / "runs"
    return sorted(root.iterdir()) if root.is_dir() else []


def final_record(running, run_id: str) -> dict[str, Any]:
    path = running.home / "runs" / run_id / "record.json"

    def load():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if "question_ids" in record else None

    return wait_for(load, what=f"the final record of run {run_id}")


def contact(running) -> dict[str, Any]:
    def load():
        for directory in run_dirs(running):
            path = directory / "work" / "contact.json"
            if path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except ValueError:
                    return None
        return None

    return wait_for(load, what="the fake agent's contact file")


def valid_submission(running) -> dict[str, Any]:
    questions = json.loads((run_dirs(running)[0] / "questions.json").read_text(encoding="utf-8"))
    answers = {}
    for question in questions:
        if question["type"] == "noul":
            answers[question["label"]] = {"p_yes": 0.4}
        else:
            keys = question["probability_keys"]
            answers[question["label"]] = {"probabilities": {key: 1.0 / len(keys) for key in keys}}
    return {"answers": answers, "rationale": "posted by the test"}


def post_form(running, url: str, body: Any, token: str | None):
    path = urllib.parse.urlsplit(url).path
    return running.client.request("POST", path, body, api_key=token)


def pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        return code.value == 259
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def grandchild_pid(running) -> int:
    def load():
        for directory in run_dirs(running):
            path = directory / "work" / "grandchild.pid"
            if path.is_file() and path.read_text(encoding="utf-8").strip():
                return int(path.read_text(encoding="utf-8"))
        return None

    return wait_for(load, what="the grandchild pid file")


def error_type(response) -> str:
    return response.body["detail"]["error_type"]


@pytest.fixture(autouse=True)
def no_sys1_threads_survive():
    yield
    wait_for(
        lambda: not [t for t in threading.enumerate() if t.name.startswith("sys1-run-")],
        timeout=20,
        what="sys1 supervisor threads to end",
    )


# -- the happy path -------------------------------------------------------------------------


class TestSuccess:
    def test_mixed_request_returns_the_pinned_shape(self, tmp_path):
        with daemon(tmp_path) as running:
            response = sys1(running)
            assert response.status == 200, response.body
            body = response.body
            assert body["model"] == "jevmulator-0.1.0-sys1-read-only-fake-fake-agent"
            assert list(body["answers"]) == ["billing", "tone", "urgency"]
            assert body["answers"]["billing"] == {"type": "noul", "noul": 0.7}
            assert body["answers"]["tone"]["choice"] == "angry"
            assert body["answers"]["urgency"]["legend"] == {"0": "Can wait", "1": "This week", "2": "Today"}
            assert body["usage"] == {"input_tokens": 100, "output_tokens": 20}
            headers = response.headers
            assert headers["X-Jevmulator-Sys1-Profile"] == "read-only"
            assert headers["X-Jevmulator-Harness"] == "fake"
            assert headers["X-Jevmulator-Sys1-Coalesced"] == "new"
            assert headers["X-Jevmulator-Usage-Source"] == "upstream"
            record = final_record(running, headers["X-Jevmulator-Sys1-Run"])
            assert record["status"] == "ACCEPTED"
            assert record["question_ids"] == {"q1": "billing", "q2": "tone", "q3": "urgency"}
            assert record["hello"]["tools"] == ["find", "grep", "ls", "read", "submit_verdict"]
            assert record["accepted"]["rationale"] == "fake agent: a scripted verdict"
            assert record["active_processes_after_close"] == 0

    def test_invalid_then_valid(self, tmp_path):
        steps = [{"do": "event"}, {"do": "submit", "kind": "sum"}, {"do": "submit", "kind": "valid"}, {"do": "settle"}]
        with daemon(tmp_path, steps) as running:
            response = sys1(running)
            assert response.status == 200, response.body
            record = final_record(running, response.headers["X-Jevmulator-Sys1-Run"])
            first, second = record["submissions"]
            assert not first["accepted"] and second["accepted"]
            assert any("sum to 0.9" in problem["problem"] for problem in first["problems"])

    def test_a_submission_still_in_flight_when_the_agent_exits_is_accepted(self, tmp_path):
        steps = [{"do": "event"}, {"do": "submit", "kind": "valid", "then_exit": True}]
        with daemon(tmp_path, steps) as running:
            response = sys1(running)
            assert response.status == 200, response.body

    def test_retry_count_header_is_logged(self, tmp_path):
        with daemon(tmp_path) as running:
            response = sys1(running, headers={"X-TypeSafe-Retry-Count": "2"})
            record = final_record(running, response.headers["X-Jevmulator-Sys1-Run"])
            assert record["attachments"][0]["retry_count"] == "2"


# -- request validation, identical to the bare path ------------------------------------------


class TestValidation:
    def test_422_body_matches_the_bare_path(self, tmp_path):
        bad = {"model": "jev-latest", "questions": {"q": {"type": "noul"}}}
        with daemon(tmp_path) as running:
            bare = running.client.post_evaluate(bad)
            harnessed = sys1(running, bad)
            assert harnessed.status == bare.status == 422
            assert harnessed.body == bare.body
            assert run_dirs(running) == []

    def test_requires_the_daemon_key(self, tmp_path):
        with daemon(tmp_path) as running:
            assert sys1(running, api_key=None).status == 401
            assert running.client.request("GET", "/sys1/v1/models", api_key=None).status == 401
            assert run_dirs(running) == []

    def test_unknown_model(self, tmp_path):
        with daemon(tmp_path) as running:
            response = sys1(running, dict(MIXED, model="gpt-4o"))
            assert response.status == 422
            detail = response.body["detail"][0]
            assert detail["type"] == "model_not_found" and detail["loc"] == ["body", "model"]

    def test_a_shell_profile_names_its_flag_while_off(self, tmp_path):
        with daemon(tmp_path) as running:
            response = sys1(running, dict(MIXED, model="sys1-prototype-first"))
            assert response.status == 422
            assert "JEVMULATOR_SYS1_ALLOW_SHELL" in response.body["detail"][0]["msg"]

    def test_zero_option_choice_starts_no_run(self, tmp_path):
        body = {"model": "jev-latest", "state": "s", "questions": {"c": {"type": "choice", "criteria": {}}}}
        with daemon(tmp_path) as running:
            response = sys1(running, body)
            assert response.status == 502 and error_type(response) == "sys1_unanswerable"
            assert run_dirs(running) == []

    def test_methods(self, tmp_path):
        with daemon(tmp_path) as running:
            assert running.client.request("GET", "/sys1/v1/systemone").status == 405
            assert running.client.request("POST", "/sys1/v1/models", {}).status == 405
            assert running.client.request("POST", "/_jevmulator/sys1/runs/" + "0" * 32 + "/other", {}).status == 404


class TestModels:
    def test_listing_without_shell(self, tmp_path):
        with daemon(tmp_path) as running:
            names = [m["name"] for m in running.client.request("GET", "/sys1/v1/models").body["models"]]
            assert names == ["jev-latest", "jev-preview", "sys1-latest", "sys1-read-only"]

    def test_listing_and_use_with_shell(self, tmp_path):
        with daemon(tmp_path, JEVMULATOR_SYS1_ALLOW_SHELL="1") as running:
            models = running.client.request("GET", "/sys1/v1/models").body["models"]
            assert "sys1-prototype-first" in [m["name"] for m in models]
            assert all("not by TypeSafe weights" in m["description"] for m in models)
            response = sys1(running, dict(MIXED, model="sys1-prototype-first"))
            assert response.status == 200
            assert response.headers["X-Jevmulator-Sys1-Profile"] == "prototype-first"


class TestHealth:
    def test_sys1_block_beside_bare_readiness(self, tmp_path):
        with daemon(tmp_path) as running:
            health = running.client.health().body
            assert health["ready"] is True
            assert health["sys1"]["ready"] is True
            assert health["sys1"]["harness"] == "fake"
            assert health["sys1"]["profiles"] == ["read-only"]

    def test_an_unready_harness_leaves_the_bare_path_ready(self, tmp_path):
        missing = str(tmp_path / "missing-script.json")
        with daemon(tmp_path, JEVMULATOR_SYS1_FAKE_SCRIPT=missing) as running:
            health = running.client.health().body
            assert health["ready"] is True and health["problems"] == []
            assert health["sys1"]["ready"] is False
            assert "missing-script.json" in health["sys1"]["problems"][0]
            response = sys1(running)
            assert response.status == 502 and error_type(response) == "sys1_harness_not_configured"
            assert running.client.post_evaluate(MIXED).status == 200

    def test_status_carries_sys1_config_and_metrics(self, tmp_path):
        with daemon(tmp_path) as running:
            sys1(running)
            status = running.client.status().body
            assert status["config"]["sys1"]["harness"] == "fake"
            assert status["metrics"]["sys1"]["runs_started"] == 1


# -- outcomes other than acceptance -----------------------------------------------------------


class TestOutcomes:
    def test_exhausted(self, tmp_path):
        steps = [{"do": "submit", "kind": "sum"}, {"do": "submit", "kind": "sum"}, {"do": "busy", "seconds": 10}]
        with daemon(tmp_path, steps, JEVMULATOR_SYS1_MAX_SUBMISSIONS="2") as running:
            response = sys1(running)
            assert response.status == 502 and error_type(response) == "sys1_invalid_submission"
            record = final_record(running, response.headers["X-Jevmulator-Sys1-Run"])
            assert record["status"] == "EXHAUSTED" and len(record["submissions"]) == 2

    def test_early_exit(self, tmp_path):
        steps = [{"do": "event"}, {"do": "exit", "code": 3}]
        with daemon(tmp_path, steps) as running:
            response = sys1(running)
            assert response.status == 502 and error_type(response) == "sys1_no_verdict"
            assert "code 3" in response.body["detail"]["message"]

    def test_timeout_carries_the_run_header(self, tmp_path):
        steps = [{"do": "busy", "seconds": 30}]
        with daemon(tmp_path, steps, JEVMULATOR_SYS1_RUN_TIMEOUT_SECONDS="1.5") as running:
            response = sys1(running)
            assert response.status == 504 and error_type(response) == "sys1_timeout"
            record = final_record(running, response.headers["X-Jevmulator-Sys1-Run"])
            assert record["status"] == "TIMED_OUT"
            assert record["active_processes_after_close"] == 0

    def test_stall(self, tmp_path):
        steps = [{"do": "sleep", "seconds": 30}]
        with daemon(tmp_path, steps, JEVMULATOR_SYS1_STALL_SECONDS="1") as running:
            response = sys1(running)
            assert response.status == 504 and error_type(response) == "sys1_stalled"

    def test_hello_with_extra_tools(self, tmp_path):
        with daemon(tmp_path, [{"do": "busy", "seconds": 10}], hello="wrong-tools") as running:
            response = sys1(running)
            assert response.status == 502 and error_type(response) == "sys1_harness_mismatch"
            assert "bash" in response.body["detail"]["message"]

    def test_hello_with_another_model(self, tmp_path):
        with daemon(tmp_path, [{"do": "busy", "seconds": 10}], hello="wrong-model") as running:
            response = sys1(running)
            assert error_type(response) == "sys1_harness_mismatch"
            assert "zai/some-other-model" in response.body["detail"]["message"]

    def test_no_hello(self, tmp_path):
        steps = [{"do": "busy", "seconds": 10}]
        with daemon(tmp_path, steps, hello="none", JEVMULATOR_SYS1_HELLO_SECONDS="1") as running:
            response = sys1(running)
            assert error_type(response) == "sys1_harness_mismatch"
            assert "reported no tools and model" in response.body["detail"]["message"]

    def test_a_model_event_naming_another_model(self, tmp_path):
        steps = [{"do": "event", "provider": "zai", "model": "other"}, {"do": "busy", "seconds": 10}]
        with daemon(tmp_path, steps) as running:
            response = sys1(running)
            assert error_type(response) == "sys1_harness_mismatch"
            assert "zai/other" in response.body["detail"]["message"]


class TestUsage:
    def test_strict_without_a_usage_event(self, tmp_path):
        steps = [{"do": "submit", "kind": "valid"}, {"do": "settle"}]
        with daemon(tmp_path, steps, JEVMULATOR_USAGE_POLICY="strict") as running:
            response = sys1(running)
            assert response.status == 502 and error_type(response) == "usage_unavailable"

    def test_strict_with_a_usage_event(self, tmp_path):
        with daemon(tmp_path, JEVMULATOR_USAGE_POLICY="strict") as running:
            assert sys1(running).status == 200

    def test_missing_usage_is_flagged_not_invented(self, tmp_path):
        steps = [{"do": "event", "usage": {"input": 0, "output": 0}}, {"do": "submit", "kind": "valid"}, {"do": "settle"}]
        with daemon(tmp_path, steps) as running:
            response = sys1(running)
            assert response.status == 200
            assert response.headers["X-Jevmulator-Usage-Source"] == "upstream-partial"
            assert response.body["usage"] == {"input_tokens": 0, "output_tokens": 0}


# -- the form's own endpoint -----------------------------------------------------------------


class TestForm:
    def test_run_token_accepted_daemon_key_refused_and_closed_after_acceptance(self, tmp_path):
        steps = [{"do": "contact"}, {"do": "busy", "seconds": 30}]
        with daemon(tmp_path, steps) as running, ThreadPoolExecutor(1) as pool:
            pending = pool.submit(sys1, running)
            info = contact(running)
            submission = valid_submission(running)
            assert post_form(running, info["submit_url"], submission, running.client.api_key).status == 401
            assert post_form(running, info["submit_url"], submission, None).status == 401
            bad = dict(submission, answers={})
            rejected = post_form(running, info["submit_url"], bad, info["token"])
            assert rejected.status == 200 and rejected.body["accepted"] is False
            assert rejected.body["attempts_left"] == 2
            accepted = post_form(running, info["submit_url"], submission, info["token"])
            assert accepted.body == {"accepted": True, "message": "Accepted. Your verdict is recorded. Stop now."}
            again = post_form(running, info["submit_url"], submission, info["token"])
            assert again.status == 409 and error_type(again) == "sys1_run_closed"
            response = pending.result(timeout=60)
            assert response.status == 200
            assert response.body["answers"]["billing"] == {"type": "noul", "noul": 0.4}

    def test_unknown_runs(self, tmp_path):
        with daemon(tmp_path) as running:
            for run_id in ("0" * 32, "not-a-run-id", "../../etc"):
                response = running.client.request(
                    "POST", f"/_jevmulator/sys1/runs/{run_id}/submission", {}, api_key="x"
                )
                assert response.status == 404, run_id

    def test_two_simultaneous_valid_submissions_accept_exactly_one(self, tmp_path):
        steps = [{"do": "contact"}, {"do": "busy", "seconds": 30}]
        with daemon(tmp_path, steps) as running, ThreadPoolExecutor(3) as pool:
            pending = pool.submit(sys1, running)
            info = contact(running)
            submission = valid_submission(running)
            barrier = threading.Barrier(2)

            def race():
                barrier.wait()
                return post_form(running, info["submit_url"], submission, info["token"])

            results = [pool.submit(race), pool.submit(race)]
            statuses = sorted(future.result(timeout=30).status for future in results)
            assert statuses == [200, 409]
            assert pending.result(timeout=60).status == 200

    def test_a_submission_after_the_timeout_gets_409(self, tmp_path):
        steps = [{"do": "contact"}, {"do": "busy", "seconds": 30}]
        with daemon(tmp_path, steps, JEVMULATOR_SYS1_RUN_TIMEOUT_SECONDS="1.5") as running, ThreadPoolExecutor(1) as pool:
            pending = pool.submit(sys1, running)
            info = contact(running)
            submission = valid_submission(running)
            assert pending.result(timeout=60).status == 504
            late = post_form(running, info["submit_url"], submission, info["token"])
            assert late.status == 409


# -- coalescing and slots ----------------------------------------------------------------------


class TestCoalescing:
    def test_identical_concurrent_requests_share_one_run(self, tmp_path):
        steps = [{"do": "busy", "seconds": 2}, {"do": "submit", "kind": "valid"}, {"do": "settle"}]
        with daemon(tmp_path, steps) as running, ThreadPoolExecutor(2) as pool:
            first = pool.submit(sys1, running)
            wait_for(lambda: run_dirs(running), what="the first run")
            second = pool.submit(sys1, running)
            responses = [first.result(timeout=60), second.result(timeout=60)]
            assert [r.status for r in responses] == [200, 200]
            assert {r.headers["X-Jevmulator-Sys1-Coalesced"] for r in responses} == {"new", "attached"}
            assert len({r.headers["X-Jevmulator-Sys1-Run"] for r in responses}) == 1
            assert len(run_dirs(running)) == 1

    def test_a_finished_run_is_replayed(self, tmp_path):
        with daemon(tmp_path) as running:
            first = sys1(running)
            started = time.monotonic()
            second = sys1(running)
            assert time.monotonic() - started < 1.0
            assert second.headers["X-Jevmulator-Sys1-Coalesced"] == "replayed"
            assert second.headers["X-Jevmulator-Sys1-Run"] == first.headers["X-Jevmulator-Sys1-Run"]
            assert second.body == first.body

    def test_reordered_labels_are_a_new_request(self, tmp_path):
        reordered = json.loads(json.dumps(MIXED))
        reordered["questions"]["tone"]["criteria"] = {"excited": "eager", "calm": None, "angry": "upset"}
        with daemon(tmp_path) as running:
            first = sys1(running)
            second = sys1(running, reordered)
            assert second.headers["X-Jevmulator-Sys1-Coalesced"] == "new"
            assert second.headers["X-Jevmulator-Sys1-Run"] != first.headers["X-Jevmulator-Sys1-Run"]

    def test_a_busy_slot_answers_429_with_retry_after(self, tmp_path):
        steps = [{"do": "busy", "seconds": 3}, {"do": "submit", "kind": "valid"}, {"do": "settle"}]
        with daemon(tmp_path, steps) as running, ThreadPoolExecutor(1) as pool:
            first = pool.submit(sys1, running)
            wait_for(lambda: run_dirs(running), what="the first run")
            other = sys1(running, dict(MIXED, state="a different ticket"))
            assert other.status == 429 and error_type(other) == "sys1_busy"
            assert int(other.headers["Retry-After"]) >= 1
            assert first.result(timeout=60).status == 200

    def test_the_bare_path_is_not_starved(self, tmp_path):
        steps = [{"do": "busy", "seconds": 3}, {"do": "submit", "kind": "valid"}, {"do": "settle"}]
        with daemon(tmp_path, steps, JEVMULATOR_MAX_INFLIGHT_REQUESTS="1") as running, ThreadPoolExecutor(1) as pool:
            pending = pool.submit(sys1, running)
            wait_for(lambda: run_dirs(running), what="the sys1 run")
            assert running.client.post_evaluate(MIXED).status == 200
            assert pending.result(timeout=60).status == 200


# -- containment and isolation -------------------------------------------------------------------


class TestContainment:
    def test_a_timeout_ends_the_grandchild(self, tmp_path):
        steps = [{"do": "grandchild", "seconds": 600}, {"do": "busy", "seconds": 30}]
        with daemon(tmp_path, steps, JEVMULATOR_SYS1_RUN_TIMEOUT_SECONDS="2") as running, ThreadPoolExecutor(1) as pool:
            pending = pool.submit(sys1, running)
            pid = grandchild_pid(running)
            assert pid_alive(pid)
            assert pending.result(timeout=60).status == 504
            wait_for(lambda: not pid_alive(pid), what="the grandchild to end")

    def test_an_orphaned_grandchild_is_ended(self, tmp_path):
        steps = [{"do": "grandchild", "seconds": 600}, {"do": "exit", "code": 0}]
        with daemon(tmp_path, steps) as running:
            response = sys1(running)
            assert error_type(response) == "sys1_no_verdict"
            pid = grandchild_pid(running)
            wait_for(lambda: not pid_alive(pid), what="the orphaned grandchild to end")

    def test_shutdown_answers_waiting_callers_with_503(self, tmp_path):
        steps = [{"do": "grandchild", "seconds": 600}, {"do": "busy", "seconds": 60}]
        with ThreadPoolExecutor(1) as pool:
            with daemon(tmp_path, steps) as running:
                pending = pool.submit(sys1, running)
                pid = grandchild_pid(running)
            response = pending.result(timeout=60)
            assert response.status == 503 and error_type(response) == "sys1_shutting_down"
            wait_for(lambda: not pid_alive(pid), what="the grandchild to end at shutdown")

    def test_the_agent_cannot_find_the_question_ids(self, tmp_path):
        ids = [f"qid-{uuid.uuid4().hex}" for _ in range(3)]
        body = {"model": "jev-latest", "state": "a ticket", "questions": dict(zip(ids, MIXED["questions"].values()))}
        report = tmp_path / "search-report.json"
        steps = [
            {"do": "event"},
            {"do": "search", "needles": ids, "report": str(report)},
            {"do": "submit", "kind": "valid"},
            {"do": "settle"},
        ]
        with daemon(tmp_path, steps) as running:
            response = sys1(running, body)
            assert response.status == 200
            assert list(response.body["answers"]) == ids
            found = json.loads(report.read_text(encoding="utf-8"))
            assert found["found"] == []

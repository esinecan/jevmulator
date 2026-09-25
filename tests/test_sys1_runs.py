"""Units of the sys1 run machinery: the registry, the fingerprint, and the event reader."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from jevmulator import errors
from jevmulator.sys1.harness.events import EventTail, digest
from jevmulator.sys1.runs import Outcome, Registry, RunState, fingerprint, prune, reconcile


class FakeRun:
    """The parts of a run the registry touches."""

    def __init__(self, key: str, run_id: str, remaining: float = 100.0) -> None:
        self.fingerprint = key
        self.run_id = run_id
        self.outcome: Outcome | None = None
        self.done = threading.Event()
        self._remaining = remaining

    @property
    def state(self) -> RunState:
        return self.outcome.state if self.outcome else RunState.RUNNING

    def remaining_seconds(self) -> float:
        return self._remaining

    def finish(self, status: int, state: RunState) -> "FakeRun":
        self.outcome = Outcome(status, {"status": status}, {}, state)
        self.done.set()
        return self


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def registry(clock=None, **kwargs) -> Registry:
    kwargs.setdefault("max_runs", 1)
    kwargs.setdefault("result_ttl_seconds", 600)
    kwargs.setdefault("failure_ttl_seconds", 120)
    return Registry(clock=clock or Clock(), **kwargs)


class TestRegistry:
    def test_new_then_attached_then_replayed(self):
        clock = Clock()
        reg = registry(clock)
        run = FakeRun("k", "a" * 32)
        assert reg.begin("k", lambda: run)[:2] == ("new", run)
        assert reg.begin("k", lambda: pytest.fail("no second run"))[:2] == ("attached", run)
        reg.decided(run.finish(200, RunState.ACCEPTED))
        kind, live, outcome, run_id = reg.begin("k", lambda: pytest.fail("replay expected"))
        assert (kind, live, outcome.status, run_id) == ("replayed", None, 200, run.run_id)

    def test_a_published_outcome_is_replayed_before_the_run_is_removed(self):
        reg = registry()
        run = FakeRun("k", "a" * 32)
        reg.begin("k", lambda: run)
        run.finish(200, RunState.ACCEPTED)
        kind, live, outcome, _run_id = reg.begin("k", lambda: pytest.fail("replay expected"))
        assert (kind, live, outcome.status) == ("replayed", None, 200)

    def test_a_decided_run_frees_its_slot(self):
        reg = registry(max_runs=1)
        run = FakeRun("k1", "a" * 32)
        reg.begin("k1", lambda: run)
        reg.decided(run.finish(200, RunState.ACCEPTED))
        other = FakeRun("k2", "b" * 32)
        assert reg.begin("k2", lambda: other)[0] == "new"

    def test_a_full_registry_answers_busy_with_retry_after(self):
        reg = registry(max_runs=1)
        reg.begin("k1", lambda: FakeRun("k1", "a" * 32, remaining=41.2))
        with pytest.raises(errors.Sys1BusyError) as caught:
            reg.begin("k2", lambda: FakeRun("k2", "b" * 32))
        assert caught.value.headers["Retry-After"] == "42"

    def test_failure_replay_expires_after_its_ttl(self):
        clock = Clock()
        reg = registry(clock)
        run = FakeRun("k", "a" * 32)
        reg.begin("k", lambda: run)
        reg.decided(run.finish(502, RunState.NO_VERDICT))
        clock.now += 119
        assert reg.begin("k", lambda: pytest.fail("replay expected"))[0] == "replayed"
        clock.now += 2
        fresh = FakeRun("k", "c" * 32)
        assert reg.begin("k", lambda: fresh)[:2] == ("new", fresh)

    def test_success_replay_lasts_longer_than_failure_replay(self):
        clock = Clock()
        reg = registry(clock)
        run = FakeRun("k", "a" * 32)
        reg.begin("k", lambda: run)
        reg.decided(run.finish(200, RunState.ACCEPTED))
        clock.now += 599
        assert reg.begin("k", lambda: pytest.fail("replay expected"))[0] == "replayed"

    def test_zero_ttl_disables_replay(self):
        reg = registry(result_ttl_seconds=0)
        run = FakeRun("k", "a" * 32)
        reg.begin("k", lambda: run)
        reg.decided(run.finish(200, RunState.ACCEPTED))
        fresh = FakeRun("k", "b" * 32)
        assert reg.begin("k", lambda: fresh)[0] == "new"

    @pytest.mark.parametrize("state", [RunState.CANCELLED, RunState.FAILED_TO_START])
    def test_outcomes_that_say_nothing_about_the_request_are_not_replayed(self, state):
        reg = registry()
        run = FakeRun("k", "a" * 32)
        reg.begin("k", lambda: run)
        reg.decided(run.finish(503, state))
        fresh = FakeRun("k", "b" * 32)
        assert reg.begin("k", lambda: fresh)[0] == "new"

    def test_closing_refuses_new_work(self):
        reg = registry()
        reg.close()
        with pytest.raises(errors.Sys1ShuttingDownError):
            reg.begin("k", lambda: FakeRun("k", "a" * 32))

    def test_find_returns_live_runs_only_by_id(self):
        reg = registry()
        run = FakeRun("k", "a" * 32)
        reg.begin("k", lambda: run)
        assert reg.find("a" * 32) is run
        assert reg.find("b" * 32) is None


class TestFingerprint:
    PAYLOAD = {"state": "s", "questions": {"t": {"type": "choice", "criteria": {"a": None, "b": None}}}}

    def test_same_request_same_fingerprint(self):
        assert fingerprint("p", self.PAYLOAD) == fingerprint("p", json.loads(json.dumps(self.PAYLOAD)))

    def test_label_order_changes_the_fingerprint(self):
        swapped = {"state": "s", "questions": {"t": {"type": "choice", "criteria": {"b": None, "a": None}}}}
        assert fingerprint("p", self.PAYLOAD) != fingerprint("p", swapped)

    def test_profile_changes_the_fingerprint_and_model_does_not(self):
        with_model = dict(self.PAYLOAD, model="jev-latest")
        assert fingerprint("p", self.PAYLOAD) == fingerprint("p", with_model)
        assert fingerprint("p", self.PAYLOAD) != fingerprint("q", self.PAYLOAD)


class TestEvents:
    def test_assistant_message_usage_and_model(self):
        info = digest({"type": "message_end", "message": {
            "role": "assistant", "provider": "zai", "model": "glm-5.3-flash",
            "usage": {"input": 900, "output": 40, "cacheRead": 100, "cacheWrite": 0, "totalTokens": 1040},
            "stopReason": "toolUse"}})
        assert (info.is_model_call, info.model, info.input_tokens, info.output_tokens) == (
            True, "zai/glm-5.3-flash", 1000, 40)
        assert info.failure is None

    def test_a_zero_usage_block_is_not_a_count(self):
        info = digest({"type": "message_end", "message": {
            "role": "assistant", "provider": "zai", "model": "m",
            "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}})
        assert info.is_model_call and info.input_tokens is None

    def test_an_error_stop_is_a_failure_note(self):
        info = digest({"type": "message_end", "message": {
            "role": "assistant", "provider": "zai", "model": "m", "stopReason": "error",
            "errorMessage": "401 invalid key"}})
        assert "401 invalid key" in info.failure

    def test_user_messages_are_not_model_calls(self):
        assert not digest({"type": "message_end", "message": {"role": "user"}}).is_model_call

    def test_settled(self):
        assert digest({"type": "agent_settled"}).settled

    def test_tail_reads_complete_lines_only(self, tmp_path: Path):
        path = tmp_path / "events.jsonl"
        tail = EventTail(path)
        assert tail.read_new() == []
        path.write_bytes(b'{"type": "a"}\n{"type": "b", "text": "\xe2\x80\xa8"}\nnot json\n{"type": "c"')
        assert [event["type"] for event in tail.read_new()] == ["a", "b"]
        with open(path, "ab") as handle:
            handle.write(b"}\n")
        assert [event["type"] for event in tail.read_new()] == ["c"]

    def test_final_read_takes_a_last_line_without_newline(self, tmp_path: Path):
        path = tmp_path / "events.jsonl"
        path.write_bytes(b'{"type": "agent_settled"}')
        tail = EventTail(path)
        assert tail.read_new() == []
        assert tail.read_new(final=True) == [{"type": "agent_settled"}]


class TestRecords:
    def test_reconcile_marks_running_records_abandoned(self, tmp_path: Path):
        run_dir = tmp_path / ("a" * 32)
        run_dir.mkdir()
        (run_dir / "record.json").write_text(json.dumps({"status": "RUNNING"}), encoding="utf-8")
        done_dir = tmp_path / ("b" * 32)
        done_dir.mkdir()
        (done_dir / "record.json").write_text(json.dumps({"status": "ACCEPTED"}), encoding="utf-8")
        assert reconcile(tmp_path) == 1
        assert json.loads((run_dir / "record.json").read_text(encoding="utf-8"))["status"] == "ABANDONED"
        assert json.loads((done_dir / "record.json").read_text(encoding="utf-8"))["status"] == "ACCEPTED"

    def test_prune_keeps_the_newest_and_skips_live_and_foreign_directories(self, tmp_path: Path):
        import os
        import time

        names = [f"{index:032x}" for index in range(5)]
        for offset, name in enumerate(names):
            directory = tmp_path / name
            directory.mkdir()
            (directory / "file.txt").write_text("x", encoding="utf-8")
            os.chmod(directory / "file.txt", 0o444)
            os.utime(directory, (time.time() + offset, time.time() + offset))
        (tmp_path / "not-a-run").mkdir()
        removed = prune(tmp_path, keep=2, live={names[0]})
        assert removed == 2
        remaining = sorted(entry.name for entry in tmp_path.iterdir())
        assert remaining == sorted([names[0], names[3], names[4], "not-a-run"])

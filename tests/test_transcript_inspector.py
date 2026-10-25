"""The native, one-file harness transcript inspector."""

from __future__ import annotations

import json
from pathlib import Path

from jevmulator.cli import main
from jevmulator.sys1.harness.transcript import inspect, render_markdown


def write_events(path: Path, events: list[object]) -> None:
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


class TestInspection:
    def test_one_stream_is_summarised_without_payload_disclosure(self, tmp_path: Path):
        path = tmp_path / "events.jsonl"
        secret = "do-not-print-this"
        write_events(path, [
            {"type": "turn_start", "timestamp": 1_700_000_001_000},
            {"type": "message_end", "timestamp": 1_700_000_002_000, "message": {
                "role": "assistant", "provider": "zai", "model": "glm", "stopReason": "toolUse",
                "usage": {"input": 10, "output": 3, "cacheRead": 20, "cacheWrite": 1,
                          "reasoning": 2, "cost": {"total": 0.001}}}},
            {"type": "tool_execution_start", "timestamp": 1_700_000_002_500, "toolCallId": "c1",
             "toolName": "read", "args": {"path": secret}},
            {"type": "tool_execution_end", "timestamp": 1_700_000_003_000, "toolCallId": "c1",
             "toolName": "read", "isError": False,
             "result": {"content": [{"type": "text", "text": "héllo " + secret},
                                     {"type": "image", "data": secret}]}},
            {"type": "agent_settled", "timestamp": 1_700_000_004_000},
        ])
        report = inspect(path)
        assert report["assistant_messages"] == 1
        assert report["turns"] == 1
        assert report["usage"] == {
            "input": 10, "output": 3, "cacheRead": 20, "cacheWrite": 1, "reasoning": 2,
        }
        assert report["final_context_tokens"] == 30
        assert report["tool_actions"] == 1
        assert report["tool_errors"] == 0
        assert report["tools"] == {"read": 1}
        assert report["tool_output_non_text_blocks"] == 1
        assert report["duration_seconds"] == 3.0
        assert report["agent_settled"] is True
        rendered = render_markdown(report)
        assert secret not in json.dumps(report)
        assert secret not in rendered

    def test_anonymous_calls_pair_by_tool_and_bad_lines_are_diagnostics(self, tmp_path: Path):
        path = tmp_path / "events.jsonl"
        path.write_text(
            '{"type":"tool_execution_start","toolName":"read"}\n'
            'not json\n'
            '{"type":"tool_execution_end","toolName":"read","isError":true}\n'
            '{"type":"tool_execution_start","toolName":"grep"}\n',
            encoding="utf-8",
        )
        report = inspect(path)
        assert report["tool_actions"] == 1
        assert report["tool_errors"] == 1
        assert [item["kind"] for item in report["diagnostics"]] == [
            "malformed_json", "unmatched_tool_start",
        ]

    def test_recorded_pi_stream_is_accepted(self):
        path = Path(__file__).parent / "fixtures" / "sys1" / "pi-0.85.1-read-only-events.jsonl"
        report = inspect(path)
        assert report["assistant_messages"] == 3
        assert report["usage"]["input"] + report["usage"]["cacheRead"] == 12331
        assert report["usage"]["output"] == 2526
        assert report["models"] == {"zai/glm-5.3-flash": 3}
        assert report["agent_settled"] is True
        assert report["tool_actions"] > 0


class TestCommand:
    def test_markdown_and_json_modes(self, tmp_path: Path, capsys):
        path = tmp_path / "events.jsonl"
        write_events(path, [{"type": "agent_settled"}])
        assert main(["inspect-transcript", str(path)]) == 0
        markdown = capsys.readouterr()
        assert "# Harness transcript inspection" in markdown.out
        assert markdown.err == ""

        assert main(["inspect-transcript", str(path), "--json"]) == 0
        output = capsys.readouterr()
        assert json.loads(output.out)["agent_settled"] is True
        assert output.err == ""

    def test_missing_and_empty_inputs_fail_cleanly(self, tmp_path: Path, capsys):
        assert main(["inspect-transcript", str(tmp_path / "missing.jsonl")]) == 3
        assert "cannot read transcript" in capsys.readouterr().err
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        assert main(["inspect-transcript", str(empty)]) == 4
        assert "no JSON event objects" in capsys.readouterr().err

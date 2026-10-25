"""Inspect one pi ``--mode json`` event transcript.

The inspector is deliberately about the event stream, not its prose.  It never emits
prompts, tool arguments or tool-result bodies.  This keeps a run record useful for
diagnosis without turning the diagnostic command into another disclosure surface.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

USAGE_KEYS = ("input", "output", "cacheRead", "cacheWrite", "reasoning")


def _number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value)


def _timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) / 1000 if value > 10_000_000_000 else float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _result_size(event: dict[str, Any]) -> tuple[int, int]:
    result = event.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return 0, 0
    text_bytes = 0
    other_blocks = 0
    for block in content:
        if not isinstance(block, dict):
            other_blocks += 1
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            text_bytes += len(block["text"].encode("utf-8"))
        else:
            other_blocks += 1
    return text_bytes, other_blocks


def inspect(path: Path, *, top: int = 10) -> dict[str, Any]:
    """Stream *path* and return a JSON-serialisable structural summary."""
    source = str(path.resolve()).replace("\\", "/")
    event_types: Counter[str] = Counter()
    models: Counter[str] = Counter()
    stops: Counter[str] = Counter()
    tools: Counter[str] = Counter()
    usage = {key: 0 for key in USAGE_KEYS}
    usage_seen: set[str] = set()
    diagnostics: list[dict[str, Any]] = []
    starts: dict[str, tuple[str, int]] = {}
    anonymous: defaultdict[str, deque[int]] = defaultdict(deque)
    outputs: list[dict[str, Any]] = []
    lines_total = records = assistant_messages = turns = tool_errors = 0
    tool_text_bytes = other_blocks = 0
    final_context: int | None = None
    settled = False
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    reported_cost = 0.0
    cost_seen = False

    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        for line_number, raw in enumerate(handle, 1):
            lines_total = line_number
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                diagnostics.append({
                    "kind": "malformed_json",
                    "line": line_number,
                    "message": "line is not valid JSON",
                })
                continue
            if not isinstance(event, dict):
                diagnostics.append({
                    "kind": "non_object",
                    "line": line_number,
                    "message": "event is not a JSON object",
                })
                continue
            records += 1
            kind = str(event.get("type", ""))
            event_types[kind or "<missing>"] += 1
            stamp = _timestamp(event.get("timestamp"))
            if stamp is not None:
                first_timestamp = stamp if first_timestamp is None else min(first_timestamp, stamp)
                last_timestamp = stamp if last_timestamp is None else max(last_timestamp, stamp)

            if kind == "turn_start":
                turns += 1
            elif kind == "agent_settled":
                settled = True
            elif kind == "message_end":
                message = event.get("message")
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    continue
                assistant_messages += 1
                provider, model = message.get("provider"), message.get("model")
                if isinstance(model, str):
                    name = f"{provider}/{model}" if isinstance(provider, str) else model
                    models[name] += 1
                stop = message.get("stopReason")
                if isinstance(stop, str):
                    stops[stop] += 1
                block = message.get("usage")
                if not isinstance(block, dict):
                    continue
                values = {key: _number(block.get(key)) for key in USAGE_KEYS}
                for key, value in values.items():
                    if value is not None:
                        usage[key] += value
                        usage_seen.add(key)
                if values["input"] is not None:
                    final_context = values["input"] + (values["cacheRead"] or 0)
                cost = block.get("cost")
                total = cost.get("total") if isinstance(cost, dict) else None
                if isinstance(total, (int, float)) and not isinstance(total, bool) and total >= 0:
                    reported_cost += float(total)
                    cost_seen = True
            elif kind == "tool_execution_start":
                tool = str(event.get("toolName") or "<unknown>")
                call_id = event.get("toolCallId")
                if isinstance(call_id, str) and call_id:
                    if call_id in starts:
                        diagnostics.append({
                            "kind": "duplicate_tool_start",
                            "line": line_number,
                            "toolCallId": call_id,
                            "message": "tool call id already has a start event",
                        })
                    else:
                        starts[call_id] = (tool, line_number)
                else:
                    anonymous[tool].append(line_number)
            elif kind == "tool_execution_end":
                tool = str(event.get("toolName") or "<unknown>")
                call_id = event.get("toolCallId")
                start_line: int | None = None
                matched_id: str | None = None
                if isinstance(call_id, str) and call_id and call_id in starts:
                    start_tool, start_line = starts.pop(call_id)
                    matched_id = call_id
                    if tool == "<unknown>":
                        tool = start_tool
                elif not call_id and anonymous[tool]:
                    start_line = anonymous[tool].popleft()
                if start_line is None:
                    diagnostics.append({
                        "kind": "unmatched_tool_end",
                        "line": line_number,
                        **({"toolCallId": call_id} if isinstance(call_id, str) else {}),
                        "message": "tool end has no matching start",
                    })
                    continue
                tools[tool] += 1
                is_error = event.get("isError") is True
                tool_errors += int(is_error)
                text_bytes, blocks = _result_size(event)
                tool_text_bytes += text_bytes
                other_blocks += blocks
                outputs.append({
                    "toolCallId": matched_id,
                    "toolName": tool,
                    "text_bytes": text_bytes,
                    "isError": is_error,
                    "start_line": start_line,
                    "end_line": line_number,
                })

    for call_id, (tool, line) in starts.items():
        diagnostics.append({
            "kind": "unmatched_tool_start",
            "line": line,
            "toolCallId": call_id,
            "message": f"{tool} start has no matching end",
        })
    for tool, queue in anonymous.items():
        for line in queue:
            diagnostics.append({
                "kind": "unmatched_tool_start",
                "line": line,
                "message": f"{tool} start has no matching end",
            })
    diagnostics.sort(key=lambda item: (item["line"], item["kind"]))
    outputs.sort(key=lambda item: (-item["text_bytes"], item["start_line"], item["toolName"]))

    return {
        "schema_version": 1,
        "source": source,
        "lines_total": lines_total,
        "records_parsed": records,
        "assistant_messages": assistant_messages,
        "turns": turns,
        "tool_actions": sum(tools.values()),
        "tool_errors": tool_errors,
        "tool_output_text_bytes": tool_text_bytes,
        "tool_output_non_text_blocks": other_blocks,
        "final_context_tokens": final_context,
        "usage": {key: usage[key] if key in usage_seen else None for key in USAGE_KEYS},
        "reported_cost": reported_cost if cost_seen else None,
        "models": dict(sorted(models.items())),
        "stop_reasons": dict(sorted(stops.items())),
        "tools": dict(sorted(tools.items())),
        "event_types": dict(sorted(event_types.items())),
        "top_tool_outputs": outputs[:top],
        "agent_settled": settled,
        "duration_seconds": (
            round(last_timestamp - first_timestamp, 3)
            if first_timestamp is not None and last_timestamp is not None and last_timestamp > first_timestamp
            else None
        ),
        "diagnostics": diagnostics,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render one inspection result without exposing transcript contents."""
    def shown(value: Any) -> str:
        return "n/a" if value is None else str(value)

    rows = [
        ("records parsed", report["records_parsed"]),
        ("turns", report["turns"]),
        ("assistant messages", report["assistant_messages"]),
        ("tool actions", report["tool_actions"]),
        ("tool errors", report["tool_errors"]),
        ("tool-output text bytes", report["tool_output_text_bytes"]),
        ("non-text result blocks", report["tool_output_non_text_blocks"]),
        ("final observed prompt tokens", report["final_context_tokens"]),
        ("reported cost", report["reported_cost"]),
        ("duration seconds", report["duration_seconds"]),
        ("agent settled", str(report["agent_settled"]).lower()),
        ("diagnostics", len(report["diagnostics"])),
    ]
    lines = [
        "# Harness transcript inspection",
        "",
        f"Source: `{report['source']}`",
        "",
        "This report describes the event stream. It does not display prompts, tool arguments, or result bodies.",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        *[f"| {name} | {shown(value)} |" for name, value in rows],
        "",
        "## Usage",
        "",
        "| Token class | Total |",
        "|---|---:|",
        *[f"| {key} | {shown(report['usage'][key])} |" for key in USAGE_KEYS],
    ]
    for heading, key in (("Models", "models"), ("Stop reasons", "stop_reasons"), ("Tools", "tools")):
        lines += ["", f"## {heading}", "", "| Name | Count |", "|---|---:|"]
        values = report[key]
        lines += [f"| `{name}` | {count} |" for name, count in values.items()] or ["| n/a | 0 |"]
    lines += [
        "",
        "## Largest tool outputs",
        "",
        "| Tool | Call ID | Bytes | Error | Lines |",
        "|---|---|---:|---|---:|",
    ]
    for item in report["top_tool_outputs"]:
        lines.append(
            f"| `{item['toolName']}` | `{item['toolCallId'] or 'anonymous'}` | "
            f"{item['text_bytes']} | {str(item['isError']).lower()} | "
            f"{item['start_line']}-{item['end_line']} |"
        )
    if not report["top_tool_outputs"]:
        lines.append("| n/a | n/a | 0 | false | n/a |")
    lines += ["", "## Diagnostics", ""]
    if report["diagnostics"]:
        for item in report["diagnostics"]:
            lines.append(f"- Line {item['line']}: `{item['kind']}` — {item['message']}")
    else:
        lines.append("No structural diagnostics.")
    return "\n".join(lines) + "\n"

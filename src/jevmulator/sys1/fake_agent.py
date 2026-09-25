"""A scripted stand-in for a harnessed agent.

The fake harness runs this file as a child process. It speaks the protocol a real harness
speaks: it posts ``hello``, writes pi-shaped JSON events to stdout, and submits through the
form. It uses only the standard library and imports nothing from jevmulator, because it
runs in a fresh interpreter with an allowlisted environment.

The JSON script named by ``SYS1_FAKE_SCRIPT`` decides what it does:

    {"hello": "correct" | "wrong-tools" | "wrong-model" | "none",
     "steps": [{"do": "event", "usage": {"input": 100, "output": 20}},
               {"do": "submit", "kind": "valid" | "sum" | "unknown-label" | "raw", "payload": {...}},
               {"do": "sleep", "seconds": 1.0},
               {"do": "busy", "seconds": 5.0},
               {"do": "grandchild", "seconds": 600},
               {"do": "contact"},
               {"do": "search", "needles": ["..."], "report": "C:/path/report.json"},
               {"do": "settle"},
               {"do": "exit", "code": 0}]}

Without a script it runs the default: one model event, one valid submission, settle, exit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_STEPS = [
    {"do": "event"},
    {"do": "submit", "kind": "valid"},
    {"do": "settle"},
]


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def log(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def post(url: str, body: object) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ.get("SYS1_RUN_TOKEN", ""),
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
        except ValueError:
            return exc.code, {}


def send_and_exit(url: str, body: object) -> None:
    import socket
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    data = json.dumps(body).encode("utf-8")
    head = (
        f"POST {parts.path} HTTP/1.1\r\n"
        f"Host: {parts.hostname}:{parts.port}\r\n"
        "Content-Type: application/json\r\n"
        f"Authorization: Bearer {os.environ.get('SYS1_RUN_TOKEN', '')}\r\n"
        f"Content-Length: {len(data)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    connection = socket.create_connection((parts.hostname, parts.port), timeout=10)
    connection.sendall(head + data)
    sys.stdout.flush()
    os._exit(0)


def load_script() -> dict:
    path = os.environ.get("SYS1_FAKE_SCRIPT", "")
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_questions() -> list[dict]:
    path = os.path.join(os.environ["SYS1_RUN_DIR"], "questions.json")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def valid_answers(questions: list[dict]) -> dict:
    answers = {}
    for question in questions:
        if question["type"] == "noul":
            answers[question["label"]] = {"p_yes": 0.7}
            continue
        keys = question["probability_keys"]
        if len(keys) == 1:
            probabilities = {keys[0]: 1.0}
        else:
            rest = 0.4 / (len(keys) - 1)
            probabilities = {key: (0.6 if index == 0 else rest) for index, key in enumerate(keys)}
        answers[question["label"]] = {"probabilities": probabilities}
    return answers


def build_submission(step: dict) -> object:
    kind = step.get("kind", "valid")
    if kind == "raw":
        return step.get("payload")
    answers = valid_answers(load_questions())
    if kind == "sum":
        for answer in answers.values():
            if "probabilities" in answer:
                answer["probabilities"] = {
                    key: value * 0.9 for key, value in answer["probabilities"].items()
                }
    elif kind == "unknown-label":
        for answer in answers.values():
            if "probabilities" in answer:
                answer["probabilities"]["not-a-label"] = 0.0
                break
    return {
        "answers": answers,
        "rationale": "fake agent: a scripted verdict",
        "evidence": ["fake_agent.py"],
    }


def search(step: dict) -> None:
    root = step.get("root") or os.environ["SYS1_RUN_DIR"]
    needles = [needle.encode("utf-8") for needle in step.get("needles", [])]
    found = []
    for directory, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(directory, name)
            try:
                with open(path, "rb") as handle:
                    content = handle.read()
            except OSError:
                continue
            for needle in needles:
                if needle in content:
                    found.append({"file": path, "needle": needle.decode("utf-8")})
    with open(step["report"], "w", encoding="utf-8") as handle:
        json.dump({"searched": root, "found": found}, handle)


def main() -> int:
    script = load_script()
    tools = [name for name in os.environ.get("SYS1_EXPECTED_TOOLS", "").split(",") if name]
    model = os.environ.get("SYS1_EXPECTED_MODEL", "fake/fake-agent")
    provider, _, model_name = model.partition("/")

    emit({"type": "session", "version": 3})
    hello = script.get("hello", "correct")
    if hello != "none":
        reported_tools, reported_model = list(tools), model
        if hello == "wrong-tools":
            reported_tools = tools + ["bash"]
        elif hello == "wrong-model":
            reported_model = "zai/some-other-model"
        status, body = post(os.environ["SYS1_HELLO_URL"], {"tools": reported_tools, "model": reported_model})
        log(f"hello -> {status} {json.dumps(body)}")

    for step in script.get("steps", DEFAULT_STEPS):
        action = step.get("do")
        if action == "event":
            usage = step.get("usage", {"input": 100, "output": 20})
            emit(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "provider": step.get("provider", provider),
                        "model": step.get("model", model_name),
                        "usage": usage,
                        "stopReason": step.get("stopReason", "toolUse"),
                    },
                }
            )
        elif action == "submit" and step.get("then_exit"):
            # Send the whole request, then exit without reading the reply, so the daemon
            # is still handling the submission when the process is gone.
            send_and_exit(os.environ["SYS1_SUBMIT_URL"], build_submission(step))
        elif action == "submit":
            status, body = post(os.environ["SYS1_SUBMIT_URL"], build_submission(step))
            log(f"submit -> {status} {json.dumps(body)}")
            emit(
                {
                    "type": "tool_execution_end",
                    "toolName": "submit_verdict",
                    "result": body,
                    "isError": not body.get("accepted", False),
                }
            )
        elif action == "sleep":
            time.sleep(float(step.get("seconds", 1)))
        elif action == "busy":
            end = time.monotonic() + float(step.get("seconds", 1))
            while time.monotonic() < end:
                emit({"type": "turn_start"})
                time.sleep(0.5)
        elif action == "grandchild":
            child = subprocess.Popen(
                [sys.executable, "-c", f"import time; time.sleep({float(step.get('seconds', 600))})"]
            )
            with open(os.path.join(os.getcwd(), "grandchild.pid"), "w", encoding="utf-8") as handle:
                handle.write(str(child.pid))
        elif action == "contact":
            with open(os.path.join(os.getcwd(), "contact.json"), "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "submit_url": os.environ["SYS1_SUBMIT_URL"],
                        "hello_url": os.environ["SYS1_HELLO_URL"],
                        "token": os.environ["SYS1_RUN_TOKEN"],
                    },
                    handle,
                    indent=2,
                )
        elif action == "search":
            search(step)
        elif action == "settle":
            emit({"type": "agent_settled"})
        elif action == "exit":
            return int(step.get("code", 0))
        else:
            log(f"unknown step {action!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

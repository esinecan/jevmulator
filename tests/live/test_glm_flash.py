"""Bounded live evaluation against the real upstream model.

This module is skipped unless ``JEVMULATOR_LIVE_TESTS=1`` is set, so an ordinary test run
never spends money and never leaves this machine.

Every case uses synthetic, nonsensitive text. The whole module is bounded by
``LIVE_CALL_CEILING`` upstream calls; once that budget is spent, the remaining cases skip
rather than continuing to spend.

Two properties are reported separately, and the distinction matters:

- **schema validity**: the answer satisfies the pinned wire schema and its own semantic
  invariants. This is what the daemon guarantees.
- **judgment correctness**: the answer agrees with what a person would say. That is a
  property of GLM Flash, not of the daemon, and a disagreement here is not a daemon defect.
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any

import pytest

from conftest import start_daemon

pytestmark = pytest.mark.live

if os.environ.get("JEVMULATOR_LIVE_TESTS") != "1":
    pytest.skip(
        "live tests are off; set JEVMULATOR_LIVE_TESTS=1 to enable them",
        allow_module_level=True,
    )

#: Hard ceiling on upstream calls for this module, per the implementation brief.
LIVE_CALL_CEILING = int(os.environ.get("JEVMULATOR_LIVE_CALL_CEILING", "30"))

#: Where the evidence file is written. Kept out of git by .gitignore.
EVIDENCE_PATH = os.environ.get(
    "JEVMULATOR_LIVE_EVIDENCE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                 "live-evidence", "glm-flash-run.json"),
)

UPSTREAM_MODEL = os.environ.get("JEVMULATOR_UPSTREAM_MODEL", "glm-5.3-flash")


class Budget:
    """Counts upstream calls across the module and stops the run at the ceiling."""

    def __init__(self, ceiling: int) -> None:
        self.ceiling = ceiling
        self.spent = 0
        self.records: list[dict[str, Any]] = []

    def check(self, needed: int) -> None:
        if self.spent + needed > self.ceiling:
            pytest.skip(
                f"live call budget exhausted: {self.spent} of {self.ceiling} spent, "
                f"{needed} more needed"
            )

    def note(self, record: dict[str, Any]) -> None:
        self.spent += record.get("upstream_calls", 0)
        self.records.append(record)


BUDGET = Budget(LIVE_CALL_CEILING)


@pytest.fixture(scope="module")
def live_daemon():
    """A daemon wired to the real upstream provider."""
    key_variable = os.environ.get("JEVMULATOR_UPSTREAM_API_KEY_ENV", "ZAI_API_KEY")
    if not os.environ.get(key_variable):
        pytest.skip(f"no upstream credential: {key_variable} is not set")

    with start_daemon(
        JEVMULATOR_PROVIDER="openai",
        JEVMULATOR_UPSTREAM_MODEL=UPSTREAM_MODEL,
        JEVMULATOR_UPSTREAM_API_KEY_ENV=key_variable,
        JEVMULATOR_UPSTREAM_RETRIES="1",
        JEVMULATOR_REPAIR_RETRIES="1",
        JEVMULATOR_UPSTREAM_TIMEOUT_SECONDS="60",
        JEVMULATOR_REQUEST_TIMEOUT_SECONDS="150",
    ) as running:
        yield running


def evaluate(daemon, request: dict[str, Any], label: str) -> dict[str, Any]:
    """One live request, with timing, usage and the upstream model recorded."""
    started = time.monotonic()
    response = daemon.client.post_evaluate(request, timeout=180)
    elapsed = time.monotonic() - started

    calls = int(response.headers.get("X-Jevmulator-Upstream-Calls", len(request["questions"])))
    record = {
        "label": label,
        "status": response.status,
        "seconds": round(elapsed, 3),
        "upstream_calls": calls,
        "upstream_model_requested": UPSTREAM_MODEL,
        "upstream_model_header": response.headers.get("X-Jevmulator-Upstream-Model"),
        "usage_source": response.headers.get("X-Jevmulator-Usage-Source"),
        "reported_model": response.body.get("model") if isinstance(response.body, dict) else None,
        "usage": response.body.get("usage") if isinstance(response.body, dict) else None,
        "answers": response.body.get("answers") if isinstance(response.body, dict) else None,
        "error": None if response.status == 200 else response.body,
    }
    BUDGET.note(record)
    return record


def assert_schema_valid(record: dict[str, Any], request: dict[str, Any]) -> None:
    """Schema and semantic validity. This is the daemon's guarantee."""
    assert record["status"] == 200, record["error"]
    answers = record["answers"]
    assert set(answers) == set(request["questions"])
    usage = record["usage"]
    assert isinstance(usage["input_tokens"], int) and usage["input_tokens"] > 0
    assert isinstance(usage["output_tokens"], int)

    for question_id, question in request["questions"].items():
        answer = answers[question_id]
        assert answer["type"] == question["type"]
        if question["type"] == "noul":
            assert 0.0 <= answer["noul"] <= 1.0
            assert "confidence" not in answer
        elif question["type"] == "choice":
            assert set(answer["probabilities"]) == set(question["criteria"])
            assert abs(sum(answer["probabilities"].values()) - 1.0) < 0.02
            best = max(answer["probabilities"].values())
            assert answer["probabilities"][answer["choice"]] == pytest.approx(best)
            assert 0.0 <= answer["confidence"] <= 1.0
        else:
            levels = len(question["criteria"])
            assert set(answer["probabilities"]) == {str(i) for i in range(levels)}
            assert set(answer["legend"]) == set(answer["probabilities"])
            expected = math.fsum(
                int(level) * value for level, value in answer["probabilities"].items()
            )
            assert answer["score"] == pytest.approx(expected, abs=1e-6)
            assert 0.0 <= answer["confidence"] <= 1.0


# -- the fixed judgment corpus -------------------------------------------
#
# Synthetic, nonsensitive text. Each case names the answer a person would give, so
# judgment correctness can be reported apart from schema validity.

CORPUS: list[dict[str, Any]] = [
    {
        "label": "billing-noul-yes",
        "request": {
            "model": "jev-latest",
            "state": "I was charged twice for invoice 1182. Please refund the duplicate.",
            "questions": {
                "billing": {"type": "noul", "instructions": "Is this message about billing?"}
            },
        },
        "expected": {"billing": ("noul_above", 0.5)},
    },
    {
        "label": "billing-noul-no",
        "request": {
            "model": "jev-latest",
            "state": "The onboarding video would not play in my browser this morning.",
            "questions": {
                "billing": {"type": "noul", "instructions": "Is this message about billing?"}
            },
        },
        "expected": {"billing": ("noul_below", 0.5)},
    },
    {
        "label": "tone-choice-angry",
        "request": {
            "model": "jev-latest",
            "state": "This is the fourth time I have written and nobody has bothered to reply.",
            "questions": {
                "tone": {
                    "type": "choice",
                    "instructions": "What is the tone of this message?",
                    "criteria": {
                        "angry": "An upset or hostile message",
                        "calm": "A neutral or polite message",
                        "excited": "An enthusiastic or eager message",
                    },
                }
            },
        },
        "expected": {"tone": ("choice_is", "angry")},
    },
    {
        "label": "tone-choice-excited",
        "request": {
            "model": "jev-latest",
            "state": "This new export feature is fantastic, I have been waiting years for it!",
            "questions": {
                "tone": {
                    "type": "choice",
                    "instructions": "What is the tone of this message?",
                    "criteria": {
                        "angry": "An upset or hostile message",
                        "calm": "A neutral or polite message",
                        "excited": "An enthusiastic or eager message",
                    },
                }
            },
        },
        "expected": {"tone": ("choice_is", "excited")},
    },
    {
        "label": "urgency-score-high",
        "request": {
            "model": "jev-latest",
            "state": "Our production payouts have been failing for three days and customers are leaving.",
            "questions": {
                "urgency": {
                    "type": "score",
                    "instructions": "How urgent is this message?",
                    "criteria": [
                        "Can wait",
                        "Needs attention this week",
                        "Needs attention today",
                    ],
                }
            },
        },
        "expected": {"urgency": ("score_above", 1.0)},
    },
    {
        "label": "urgency-score-low",
        "request": {
            "model": "jev-latest",
            "state": "Some day it would be nice if the settings page remembered my sort order.",
            "questions": {
                "urgency": {
                    "type": "score",
                    "instructions": "How urgent is this message?",
                    "criteria": [
                        "Can wait",
                        "Needs attention this week",
                        "Needs attention today",
                    ],
                }
            },
        },
        "expected": {"urgency": ("score_below", 1.0)},
    },
    {
        "label": "mixed-structured",
        "request": {
            "model": "jev-latest",
            "state": {
                "subject": "Duplicate charge on invoice 1182",
                "body": "I was billed twice this month. Please refund one of the charges.",
                "tags": ["billing", "refund"],
            },
            "questions": {
                "billing": {"type": "noul", "instructions": {"task": "Is this about billing?"}},
                "tone": {
                    "type": "choice",
                    "instructions": "What is the tone?",
                    "criteria": {"angry": "upset", "calm": None, "excited": {"note": "eager"}},
                },
                "urgency": {
                    "type": "score",
                    "instructions": "How urgent is this?",
                    "criteria": ["Can wait", "This week", ["Needs", "attention", "today"]],
                },
            },
        },
        "expected": {"billing": ("noul_above", 0.5)},
    },
]


def judge(answers: dict[str, Any], expected: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare the live answers against the corpus expectations."""
    verdicts = []
    for question_id, (kind, target) in expected.items():
        answer = answers[question_id]
        if kind == "noul_above":
            ok = answer["noul"] > target
            actual = answer["noul"]
        elif kind == "noul_below":
            ok = answer["noul"] < target
            actual = answer["noul"]
        elif kind == "choice_is":
            ok = answer["choice"] == target
            actual = answer["choice"]
        elif kind == "score_above":
            ok = answer["score"] > target
            actual = answer["score"]
        elif kind == "score_below":
            ok = answer["score"] < target
            actual = answer["score"]
        else:
            raise AssertionError(f"unknown expectation {kind}")
        verdicts.append(
            {"question": question_id, "rule": kind, "target": target, "actual": actual, "agrees": ok}
        )
    return verdicts


@pytest.mark.parametrize("case", CORPUS, ids=[case["label"] for case in CORPUS])
def test_the_corpus_answers_are_schema_valid(live_daemon, case) -> None:
    """The daemon's guarantee. A failure here is a daemon defect."""
    BUDGET.check(len(case["request"]["questions"]))
    record = evaluate(live_daemon, case["request"], case["label"])
    assert_schema_valid(record, case["request"])
    record["judgment"] = judge(record["answers"], case["expected"])


def test_the_upstream_model_is_the_configured_one(live_daemon) -> None:
    BUDGET.check(1)
    request = {
        "model": "jev-latest",
        "state": "A short synthetic message.",
        "questions": {"q": {"type": "noul", "instructions": "Is this a message?"}},
    }
    record = evaluate(live_daemon, request, "model-identity")
    assert record["status"] == 200, record["error"]
    assert record["upstream_model_header"] == UPSTREAM_MODEL
    assert record["reported_model"].endswith(UPSTREAM_MODEL)


def test_the_upstream_reports_real_token_counts(live_daemon) -> None:
    BUDGET.check(1)
    request = {
        "model": "jev-latest",
        "state": "A short synthetic message about a failed payout.",
        "questions": {"q": {"type": "noul", "instructions": "Is this about a payout?"}},
    }
    record = evaluate(live_daemon, request, "usage-reporting")
    assert record["status"] == 200, record["error"]
    assert record["usage_source"] == "upstream"
    assert record["usage"]["input_tokens"] > 0


def test_bounded_concurrency_holds_under_a_multi_question_request(live_daemon) -> None:
    """Several questions in one request, still one upstream call each."""
    questions = {
        f"fact_{index}": {
            "type": "noul",
            "instructions": f"Does the message mention item number {index}?",
        }
        for index in range(4)
    }
    BUDGET.check(len(questions))
    request = {
        "model": "jev-latest",
        "state": "The order contained item number 1 and item number 3.",
        "questions": questions,
    }
    record = evaluate(live_daemon, request, "concurrency-4")
    assert record["status"] == 200, record["error"]
    assert record["upstream_calls"] == 4
    assert set(record["answers"]) == set(questions)


def test_a_deliberately_impossible_model_name_is_not_silently_substituted() -> None:
    """A wrong upstream model must fail, never fall back to a different one."""
    key_variable = os.environ.get("JEVMULATOR_UPSTREAM_API_KEY_ENV", "ZAI_API_KEY")
    if not os.environ.get(key_variable):
        pytest.skip(f"no upstream credential: {key_variable} is not set")
    BUDGET.check(1)
    with start_daemon(
        JEVMULATOR_PROVIDER="openai",
        JEVMULATOR_UPSTREAM_MODEL="glm-model-that-does-not-exist",
        JEVMULATOR_UPSTREAM_API_KEY_ENV=key_variable,
        JEVMULATOR_UPSTREAM_RETRIES="0",
        JEVMULATOR_REPAIR_RETRIES="0",
    ) as running:
        record = evaluate(
            running,
            {
                "model": "jev-latest",
                "state": "A short synthetic message.",
                "questions": {"q": {"type": "noul", "instructions": "Is this a message?"}},
            },
            "unknown-upstream-model",
        )
    assert record["status"] in (502, 504, 529), record
    assert record["answers"] is None


def test_write_the_evidence_file() -> None:
    """Runs last. Writes timings, usage and judgment verdicts for the test report."""
    os.makedirs(os.path.dirname(EVIDENCE_PATH), exist_ok=True)
    successes = [record for record in BUDGET.records if record["status"] == 200]
    judgments = [
        verdict
        for record in BUDGET.records
        for verdict in record.get("judgment", [])
    ]
    summary = {
        "upstream_model": UPSTREAM_MODEL,
        "call_ceiling": LIVE_CALL_CEILING,
        "upstream_calls_spent": BUDGET.spent,
        "requests": len(BUDGET.records),
        "requests_succeeded": len(successes),
        "input_tokens_total": sum(
            record["usage"]["input_tokens"] for record in successes if record["usage"]
        ),
        "output_tokens_total": sum(
            record["usage"]["output_tokens"] for record in successes if record["usage"]
        ),
        "seconds_total": round(sum(record["seconds"] for record in BUDGET.records), 3),
        "seconds_max": round(max((r["seconds"] for r in BUDGET.records), default=0.0), 3),
        "judgment_checks": len(judgments),
        "judgment_agreements": sum(1 for verdict in judgments if verdict["agrees"]),
        "records": BUDGET.records,
    }
    with open(EVIDENCE_PATH, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"\nlive evidence written to {EVIDENCE_PATH}")
    print(
        json.dumps(
            {key: value for key, value in summary.items() if key != "records"},
            ensure_ascii=False,
        )
    )
    assert BUDGET.spent <= LIVE_CALL_CEILING

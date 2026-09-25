"""The sys1 form: the one surface an agent submits its verdict through.

The form is harness-neutral. A harness exposes it to its agent as a tool, and the tool
posts the agent's arguments here unchanged. Every rule lives in this module, so every
harness gets the same checks: the object shape, the exact question labels, then each
answer through the same builders the bare path uses. The agent supplies distributions
only; the daemon computes every derived field.

Question IDs are routing labels that the pinned contract keeps out of model input. The
agent therefore sees opaque aliases, ``q1`` to ``qN`` in request order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import answers, prompts, wire

ALIAS_PREFIX = "q"
FORM_FIELDS = ("answers", "rationale", "evidence")
MAX_RATIONALE_CHARS = 20_000
MAX_EVIDENCE_ITEMS = 100
MAX_EVIDENCE_ITEM_CHARS = 2_000


@dataclass(frozen=True)
class AliasedRequest:
    """A request's questions under opaque labels.

    ``question_ids`` holds the caller's IDs. It stays in memory while a run lives and is
    never written where the agent can read it.
    """

    aliases: tuple[str, ...]
    questions: tuple[wire.Question, ...]
    question_ids: tuple[str, ...]

    def by_alias(self) -> dict[str, wire.Question]:
        return dict(zip(self.aliases, self.questions))

    def id_for(self, alias: str) -> str:
        return self.question_ids[self.aliases.index(alias)]


def alias_request(request: wire.SystemOneRequest) -> AliasedRequest:
    ids = tuple(request.question_ids)
    aliases = tuple(f"{ALIAS_PREFIX}{index}" for index in range(1, len(ids) + 1))
    return AliasedRequest(
        aliases=aliases,
        questions=tuple(request.questions[question_id] for question_id in ids),
        question_ids=ids,
    )


def unanswerable_aliases(aliased: AliasedRequest) -> list[str]:
    """Labels no distribution can satisfy: a choice with zero options.

    The pinned schema accepts such a choice (``wire.py``), but an empty distribution is
    always rejected (``primitives.check_probabilities``), so no submission could ever pass.
    """
    return [
        alias
        for alias, question in zip(aliased.aliases, aliased.questions)
        if isinstance(question, wire.ChoiceQuestion) and not question.labels
    ]


def answer_schema(question: wire.Question) -> dict[str, Any]:
    """The JSON Schema of one answer object, shared with the bare path's upstream schema."""
    return prompts.output_schema(question)["properties"][prompts.ANSWER_KEY]


def answer_keys(question: wire.Question) -> list[str]:
    if isinstance(question, wire.ChoiceQuestion):
        return question.labels
    if isinstance(question, wire.ScoreQuestion):
        return question.level_keys
    return []


def submission_schema(aliased: AliasedRequest) -> dict[str, Any]:
    """The exact JSON Schema of an acceptable submission for this request."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "sys1 submission",
        "type": "object",
        "properties": {
            "answers": {
                "type": "object",
                "properties": {
                    alias: answer_schema(question)
                    for alias, question in zip(aliased.aliases, aliased.questions)
                },
                "required": list(aliased.aliases),
                "additionalProperties": False,
            },
            "rationale": {"type": "string", "minLength": 1, "maxLength": MAX_RATIONALE_CHARS},
            "evidence": {
                "type": "array",
                "items": {"type": "string", "maxLength": MAX_EVIDENCE_ITEM_CHARS},
                "maxItems": MAX_EVIDENCE_ITEMS,
            },
        },
        "required": ["answers", "rationale"],
        "additionalProperties": False,
    }


def questions_document(aliased: AliasedRequest) -> list[dict[str, Any]]:
    """The questions as the agent may read them from disk: labels, never caller IDs."""
    document = []
    for alias, question in zip(aliased.aliases, aliased.questions):
        entry: dict[str, Any] = {
            "label": alias,
            "type": question.type,
            "instructions": question.instructions,
            "criteria": question.criteria,
            "answer_schema": answer_schema(question),
        }
        keys = answer_keys(question)
        if keys:
            entry["probability_keys"] = keys
        document.append(entry)
    return document


@dataclass
class FormResult:
    accepted: bool
    problems: list[dict[str, str]] = field(default_factory=list)
    answers: dict[str, dict[str, Any]] | None = None
    rationale: str | None = None
    evidence: list[str] | None = None


def validate_submission(
    payload: Any,
    aliased: AliasedRequest,
    *,
    normalize: bool,
    tolerance: float,
    max_sum_error: float,
) -> FormResult:
    """Check one submission and build the wire answers when it passes.

    Every problem found is returned at once, so the agent can fix them together. The
    answer builders stop at the first problem inside one answer, so each label reports at
    most one problem.
    """
    problems: list[dict[str, str]] = []

    def problem(path: str, text: str) -> None:
        problems.append({"path": path, "problem": text})

    if not isinstance(payload, dict):
        problem("", "the submission must be a JSON object with answers, rationale and evidence")
        return FormResult(accepted=False, problems=problems)

    for key in payload:
        if key not in FORM_FIELDS:
            problem(key, f"unknown field {key!r}; the form accepts answers, rationale and evidence")

    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        problem("rationale", "rationale must be a non-empty string saying what you checked")
    elif len(rationale) > MAX_RATIONALE_CHARS:
        problem("rationale", f"rationale is {len(rationale)} characters; the limit is {MAX_RATIONALE_CHARS}")

    evidence = payload.get("evidence")
    if evidence is not None:
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            problem("evidence", "evidence must be a list of strings")
        elif len(evidence) > MAX_EVIDENCE_ITEMS:
            problem("evidence", f"evidence has {len(evidence)} items; the limit is {MAX_EVIDENCE_ITEMS}")
        elif any(len(item) > MAX_EVIDENCE_ITEM_CHARS for item in evidence):
            problem("evidence", f"an evidence item is longer than {MAX_EVIDENCE_ITEM_CHARS} characters")

    built: dict[str, dict[str, Any]] = {}
    raw_answers = payload.get("answers")
    if not isinstance(raw_answers, dict):
        problem("answers", "answers must be an object with one entry per label: " + ", ".join(aliased.aliases))
    else:
        for key in raw_answers:
            if key not in aliased.aliases:
                problem(
                    f"answers.{key}",
                    f"{key!r} is not a question label; the labels are " + ", ".join(aliased.aliases),
                )
        for alias, question in zip(aliased.aliases, aliased.questions):
            path = f"answers.{alias}"
            if alias not in raw_answers:
                problem(path, f"the answer for {alias} is missing")
                continue
            answer = raw_answers[alias]
            if not isinstance(answer, dict):
                problem(path, f"the answer for {alias} must be an object")
                continue
            try:
                built[alias] = answers.build_answer(
                    question,
                    answer,
                    normalize=normalize,
                    tolerance=tolerance,
                    max_sum_error=max_sum_error,
                )
            except answers.AnswerRejected as exc:
                problem(path, str(exc))

    if problems:
        return FormResult(accepted=False, problems=problems)
    return FormResult(
        accepted=True,
        answers=built,
        rationale=rationale,
        evidence=list(evidence) if evidence is not None else None,
    )

"""Pinned wire types: request validation and response assembly.

This module is the only place that decides what the pinned TypeSafe OpenAPI 3.1.0
snapshot accepts and emits. Validation follows the published component schemas exactly.
Where the documentation prose is stricter than the schema, the schema wins and the prose
is recorded in docs/compatibility.md instead of being enforced here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import RequestValidationError, validation_detail

#: The three question and answer discriminator values.
QUESTION_TYPES = ("noul", "choice", "score")

CONTENT_DESCRIPTION = "Input should be a valid string, object or array"


def is_content(value: Any) -> bool:
    """True for the pinned ``content`` union: string, object, or array.

    A top-level number, boolean or null is not content. Nested numbers, booleans and
    nulls inside an object or array are unrestricted.
    """
    return isinstance(value, (str, dict, list))


def is_content_or_null(value: Any) -> bool:
    return value is None or is_content(value)


@dataclass(frozen=True)
class NoulQuestion:
    instructions: Any = None
    criteria: dict[str, Any] | None = None
    type: str = "noul"


@dataclass(frozen=True)
class ChoiceQuestion:
    criteria: dict[str, Any] = None  # type: ignore[assignment]
    instructions: Any = None
    type: str = "choice"

    @property
    def labels(self) -> list[str]:
        """Option labels in the order the request supplied them."""
        return list(self.criteria.keys())


@dataclass(frozen=True)
class ScoreQuestion:
    criteria: list[Any] = None  # type: ignore[assignment]
    instructions: Any = None
    type: str = "score"

    @property
    def level_keys(self) -> list[str]:
        """Zero-based level indices as the string keys used on the wire."""
        return [str(index) for index in range(len(self.criteria))]

    def legend(self) -> dict[str, Any]:
        """Original level descriptions keyed by level, with structure preserved."""
        return {str(index): value for index, value in enumerate(self.criteria)}


Question = NoulQuestion | ChoiceQuestion | ScoreQuestion


@dataclass(frozen=True)
class SystemOneRequest:
    state: Any
    model: str
    questions: dict[str, Question]

    @property
    def question_ids(self) -> list[str]:
        return list(self.questions.keys())


def _reject(details: list[dict[str, Any]]) -> None:
    raise RequestValidationError(details)


def parse_request(payload: Any) -> SystemOneRequest:
    """Validate one decoded JSON body against the pinned ``SystemOneRequest``.

    Args:
        payload: The decoded request body.

    Returns:
        The validated request.

    Raises:
        RequestValidationError: The body does not satisfy the pinned schema. The error
            carries pinned ``ValidationError`` entries whose ``loc`` starts at ``body``.
    """
    details: list[dict[str, Any]] = []

    if not isinstance(payload, dict):
        _reject([validation_detail(["body"], "Input should be a valid dictionary", "dict_type")])

    for field_name in ("state", "model", "questions"):
        if field_name not in payload:
            details.append(
                validation_detail(["body", field_name], "Field required", "missing")
            )
    if details:
        _reject(details)

    state = payload["state"]
    if not is_content(state):
        details.append(
            validation_detail(["body", "state"], CONTENT_DESCRIPTION, "content_type", input_=state)
        )

    model = payload["model"]
    if not isinstance(model, str):
        details.append(
            validation_detail(
                ["body", "model"], "Input should be a valid string", "string_type", input_=model
            )
        )

    raw_questions = payload["questions"]
    if not isinstance(raw_questions, dict):
        details.append(
            validation_detail(
                ["body", "questions"],
                "Input should be a valid dictionary",
                "dict_type",
                input_=raw_questions,
            )
        )
        _reject(details)
    elif not raw_questions:
        details.append(
            validation_detail(
                ["body", "questions"],
                "Dictionary should have at least 1 item after validation, not 0",
                "too_short",
                ctx={"field_type": "Dictionary", "min_length": 1, "actual_length": 0},
            )
        )

    questions: dict[str, Question] = {}
    for question_id, raw in raw_questions.items():
        loc = ["body", "questions", question_id]
        parsed = _parse_question(raw, loc, details)
        if parsed is not None:
            questions[question_id] = parsed

    if details:
        _reject(details)

    return SystemOneRequest(state=state, model=model, questions=questions)


def _parse_question(raw: Any, loc: list[Any], details: list[dict[str, Any]]) -> Question | None:
    if not isinstance(raw, dict):
        details.append(
            validation_detail(loc, "Input should be a valid dictionary", "dict_type", input_=raw)
        )
        return None

    if "type" not in raw:
        details.append(
            validation_detail(
                loc + ["type"],
                "Unable to extract tag using discriminator 'type'",
                "union_tag_not_found",
                ctx={"discriminator": "'type'"},
                input_=raw,
            )
        )
        return None

    question_type = raw["type"]
    if question_type not in QUESTION_TYPES:
        details.append(
            validation_detail(
                loc,
                "Input tag '%s' found using 'type' does not match any of the expected tags: "
                "'noul', 'choice', 'score'" % (question_type,),
                "union_tag_invalid",
                ctx={"discriminator": "'type'", "tag": str(question_type),
                     "expected_tags": "'noul', 'choice', 'score'"},
                input_=raw,
            )
        )
        return None

    if "instructions" in raw and not is_content_or_null(raw["instructions"]):
        details.append(
            validation_detail(
                loc + [question_type, "instructions"],
                CONTENT_DESCRIPTION,
                "content_type",
                input_=raw["instructions"],
            )
        )
    instructions = raw.get("instructions")

    if question_type == "noul":
        criteria = raw.get("criteria")
        if criteria is not None:
            if not isinstance(criteria, dict):
                details.append(
                    validation_detail(
                        loc + ["noul", "criteria"],
                        "Input should be a valid dictionary",
                        "dict_type",
                        input_=criteria,
                    )
                )
                return None
            for key in criteria:
                if key not in ("true", "false"):
                    details.append(
                        validation_detail(
                            loc + ["noul", "criteria", key],
                            "Extra inputs are not permitted",
                            "extra_forbidden",
                            input_=criteria[key],
                        )
                    )
            for key in ("true", "false"):
                if key in criteria and not is_content_or_null(criteria[key]):
                    details.append(
                        validation_detail(
                            loc + ["noul", "criteria", key],
                            CONTENT_DESCRIPTION,
                            "content_type",
                            input_=criteria[key],
                        )
                    )
        return NoulQuestion(instructions=instructions, criteria=criteria)

    if "criteria" not in raw:
        details.append(
            validation_detail(loc + [question_type, "criteria"], "Field required", "missing")
        )
        return None
    criteria = raw["criteria"]

    if question_type == "choice":
        if not isinstance(criteria, dict):
            details.append(
                validation_detail(
                    loc + ["choice", "criteria"],
                    "Input should be a valid dictionary",
                    "dict_type",
                    input_=criteria,
                )
            )
            return None
        for label, description in criteria.items():
            if not is_content_or_null(description):
                details.append(
                    validation_detail(
                        loc + ["choice", "criteria", label],
                        CONTENT_DESCRIPTION,
                        "content_type",
                        input_=description,
                    )
                )
        return ChoiceQuestion(criteria=criteria, instructions=instructions)

    if not isinstance(criteria, list):
        details.append(
            validation_detail(
                loc + ["score", "criteria"],
                "Input should be a valid list",
                "list_type",
                input_=criteria,
            )
        )
        return None
    if len(criteria) < 1:
        details.append(
            validation_detail(
                loc + ["score", "criteria"],
                "List should have at least 1 item after validation, not 0",
                "too_short",
                ctx={"field_type": "List", "min_length": 1, "actual_length": 0},
            )
        )
        return None
    for index, level in enumerate(criteria):
        if not is_content(level):
            details.append(
                validation_detail(
                    loc + ["score", "criteria", index],
                    CONTENT_DESCRIPTION,
                    "content_type",
                    input_=level,
                )
            )
    return ScoreQuestion(criteria=criteria, instructions=instructions)


# -- response assembly ---------------------------------------------------


def noul_answer(probability_yes: float) -> dict[str, Any]:
    """Build the pinned ``NoulAnswer``. There is no confidence field."""
    return {"type": "noul", "noul": float(probability_yes)}


def choice_answer(
    choice: str, confidence: float, probabilities: dict[str, float]
) -> dict[str, Any]:
    """Build the pinned ``ChoiceAnswer``."""
    return {
        "type": "choice",
        "choice": choice,
        "confidence": float(confidence),
        "probabilities": {label: float(value) for label, value in probabilities.items()},
    }


def score_answer(
    score: float, confidence: float, legend: dict[str, Any], probabilities: dict[str, float]
) -> dict[str, Any]:
    """Build the pinned ``ScoreAnswer``. ``legend`` keeps the original structure."""
    return {
        "type": "score",
        "score": float(score),
        "confidence": float(confidence),
        "legend": legend,
        "probabilities": {level: float(value) for level, value in probabilities.items()},
    }


def system_one_response(
    model: str, answers: dict[str, dict[str, Any]], input_tokens: int, output_tokens: int
) -> dict[str, Any]:
    """Build the pinned ``SystemOneResponse``.

    ``usage`` carries required integers, as the wire schema demands. The counts come from
    the upstream provider; see docs/compatibility.md on what they do and do not mean.
    """
    return {
        "model": model,
        "answers": answers,
        "usage": {"input_tokens": int(input_tokens), "output_tokens": int(output_tokens)},
    }


def model_metadata_list(models: list[dict[str, str]]) -> dict[str, Any]:
    """Build the pinned ``ModelMetadataList``."""
    return {"models": models}

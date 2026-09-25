"""Render a profile's brief for one run.

The brief is the agent's whole system prompt. It carries the state, the questions under
their opaque labels, the exact answer shape for each label, and the rules of the form. It
never carries a caller's question ID.
"""

from __future__ import annotations

import json
from string import Template
from typing import Any

from .. import wire
from ..prompts import _render_content
from .form import AliasedRequest, answer_keys
from .profiles import Profile

#: A state longer than this is referenced by its file instead of repeated in the brief.
STATE_INLINE_LIMIT = 20_000

_DEFAULT_QUESTION = {
    "noul": "Decide whether the statement implied by the criteria is true of the state.",
    "choice": (
        "Choose the option that best describes the state. Each option is interpreted by "
        "its name and its description."
    ),
    "score": "Rate the state against the ordered levels.",
}


def render_state_block(state: Any) -> str:
    text = _render_content(state)
    if len(text) > STATE_INLINE_LIMIT:
        return (
            f"The state is {len(text)} characters long, too long to repeat here. Read it "
            "from the file named above."
        )
    return "<state>\n" + text + "\n</state>"


def _render_question(alias: str, question: wire.Question) -> str:
    lines = [f"### {alias} ({question.type})", ""]
    if question.instructions is None:
        lines.append("Question: " + _DEFAULT_QUESTION[question.type])
    else:
        lines.append("Question: " + _render_content(question.instructions))

    if isinstance(question, wire.NoulQuestion):
        criteria = question.criteria or {}
        if criteria.get("true") is not None:
            lines.append("yes means: " + _render_content(criteria["true"]))
        if criteria.get("false") is not None:
            lines.append("no means: " + _render_content(criteria["false"]))
        lines.append("")
        lines.append(
            'Answer shape: {"p_yes": <the probability, from 0 to 1, that the answer is yes>}'
        )
        return "\n".join(lines)

    keys = answer_keys(question)
    lines.append("")
    if isinstance(question, wire.ChoiceQuestion):
        lines.append("Options:")
        for label, description in question.criteria.items():
            if description is None:
                lines.append(f"- {json.dumps(label, ensure_ascii=False)}: no description, so judge it by its name")
            else:
                lines.append(f"- {json.dumps(label, ensure_ascii=False)}: {_render_content(description)}")
    else:
        lines.append("Levels, from the lowest (0) to the highest:")
        for index, level in enumerate(question.criteria):
            lines.append(f'- "{index}": {_render_content(level)}')
    shape = ", ".join(f"{json.dumps(key, ensure_ascii=False)}: <probability>" for key in keys)
    lines.append("")
    lines.append('Answer shape: {"probabilities": {' + shape + "}}")
    lines.append(
        "The probabilities object has exactly these keys and no others: "
        + json.dumps(keys, ensure_ascii=False)
    )
    return "\n".join(lines)


def render_questions(aliased: AliasedRequest) -> str:
    return "\n\n".join(
        _render_question(alias, question)
        for alias, question in zip(aliased.aliases, aliased.questions)
    )


def render_brief(
    profile: Profile,
    aliased: AliasedRequest,
    state: Any,
    *,
    state_path: str,
    workdir: str,
    tool_names: list[str],
    max_submissions: int,
    max_sum_error: float,
) -> str:
    if profile.read_roots:
        read_roots_note = (
            "You can read only inside these directories: "
            + ", ".join(f"`{root}`" for root in profile.read_roots)
            + "."
        )
    else:
        read_roots_note = "You can read files anywhere the state points."
    return Template(profile.brief_template).substitute(
        workdir=workdir,
        read_roots_note=read_roots_note,
        tool_names=", ".join(tool_names),
        state_path=state_path,
        state_block=render_state_block(state),
        questions=render_questions(aliased),
        max_submissions=max_submissions,
        max_sum_error=max_sum_error,
    )

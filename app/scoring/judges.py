from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.providers.contract import Message, ModelResponse
from app.scoring.checks import Outcome
from app.scoring.judge_prompts import (
    JudgeReplyError,
    Transcript,
    checklist_messages,
    claim_messages,
    locate,
    parse_answers,
    parse_claims,
)
from app.scoring.run_view import RunView

# LLM judges. Unlike the deterministic checks they cost money and can be wrong, so every
# answer must quote the trace, a doubtful one says "can't tell", and a question only counts
# once human labels show the judge agrees with people on it.

DEFAULT_JUDGE_MODEL = "gemini-3.5-flash-lite"
DEFAULT_JUDGE_THRESHOLD = 0.7
# A run whose score is this close to the threshold is asked again, so one shaky answer
# does not decide it.
BORDERLINE_MARGIN = 0.2
ASKS_WHEN_BORDERLINE = 3

Answer = Literal["yes", "no", "cant_tell"]
Ask = Callable[[list[Message]], Awaitable[ModelResponse]]


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=60, pattern=r"^[a-z0-9_]+$")
    text: str = Field(min_length=10, max_length=500)


class Example(BaseModel):
    """A run a person labelled, shown to the judge as a worked answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_id: str
    answer: Literal["yes", "no"]
    excerpt: str = Field(max_length=3000)


PACKS: dict[str, list[Question]] = {
    "support": [
        Question(id="answers_the_request", text="Does the final reply answer what the user asked?"),
        Question(
            id="no_unbacked_promises",
            text="Does the reply avoid promising anything the tool results do not support?",
        ),
        Question(id="polite", text="Is the reply polite and free of blame toward the user?"),
    ],
    "tool_use": [
        Question(
            id="looked_things_up",
            text="Did the agent call a tool whenever it needed information it did not have?",
        ),
        Question(
            id="used_the_results",
            text="Does the final reply use what the tool calls returned?",
        ),
        Question(
            id="no_repeated_calls",
            text="Did the agent avoid calling the same tool with the same arguments twice?",
        ),
    ],
    "retrieval": [
        Question(
            id="grounded",
            text="Does the reply rely on the retrieved content rather than general knowledge?",
        ),
        Question(
            id="admits_gaps",
            text="Does the reply say so when the retrieved content does not answer the question?",
        ),
        Question(
            id="quotes_accurately",
            text="Is everything the reply attributes to retrieved content stated accurately?",
        ),
    ],
}

CLAIMS_QUESTION = Question(
    id="claims_supported",
    text="Is every factual claim in the final reply backed by the tool results?",
)


class _JudgeParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Pinned per scorer version, so changing the default never changes an old judge.
    model: str = Field(default=DEFAULT_JUDGE_MODEL, min_length=1, max_length=100)
    # Judges are soft. Making one hard is allowed, but only on purpose.
    allow_hard: bool = False
    # Filled from human labels when the spec is saved.
    examples: list[Example] = Field(default_factory=list, max_length=20)


class ChecklistParams(_JudgeParams):
    questions: list[Question] = Field(default_factory=list, max_length=5)
    pack: Literal["support", "tool_use", "retrieval"] | None = None

    @model_validator(mode="after")
    def _questions_or_pack(self) -> "ChecklistParams":
        # A stored pack judge holds the pack and its written-out questions; anything else
        # with both would be ambiguous.
        stored_pack = self.pack is not None and self.questions == PACKS[self.pack]
        if bool(self.questions) == (self.pack is not None) and not stored_pack:
            raise ValueError("give either questions (1 to 5) or a pack, not both")
        if self.pack is not None:
            # The pack's questions are written out, so the scorer's hash is what it asks.
            object.__setattr__(self, "questions", PACKS[self.pack])
        ids = [q.id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("question ids must be unique")
        return self


class ClaimCheckParams(_JudgeParams):
    pass


JUDGES: dict[str, type[_JudgeParams]] = {
    "checklist": ChecklistParams,
    "claim_check": ClaimCheckParams,
}


def questions_of(params: _JudgeParams) -> list[Question]:
    if isinstance(params, ChecklistParams):
        return params.questions
    return [CLAIMS_QUESTION]


@dataclass(frozen=True)
class QuestionResult:
    question: Question
    answer: Answer
    quote: str | None
    evidence: tuple[str, ...]
    # How often repeated asks agreed. None when the run was asked once.
    confidence: float | None = None


@dataclass(frozen=True)
class Claim:
    text: str
    verdict: Literal["supported", "contradicted", "unverified"]
    quote: str | None
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class JudgeResult:
    answers: list[QuestionResult]
    model_version: str
    borderline: bool = False
    claims: list[Claim] | None = None


async def _ask_parsed[T](
    ask: Ask, messages: list[Message], parse: Callable[[str], T]
) -> tuple[T, str]:
    # One more try on a reply that is not the JSON asked for; models slip now and then.
    for attempt in range(2):
        response = await ask(messages)
        try:
            return parse(response.text), response.resolved_model
        except JudgeReplyError:
            if attempt == 1:
                raise
    raise AssertionError("unreachable")


def _checked(
    transcript: Transcript, questions: list[Question], raw: list[tuple[str, str, str | None]]
) -> list[QuestionResult]:
    given = {qid: (answer, quote) for qid, answer, quote in raw}
    results = []
    for question in questions:
        answer, quote = given.get(question.id, ("cant_tell", None))
        evidence = locate(quote, transcript) if quote else ()
        # An answer without a quote found in the run is a guess, so it does not count.
        if answer != "cant_tell" and not evidence:
            answer, quote = "cant_tell", None
        results.append(QuestionResult(question, answer, quote, evidence))  # type: ignore[arg-type]
    return results


def _score(answers: list[QuestionResult]) -> float | None:
    answered = [a for a in answers if a.answer != "cant_tell"]
    if not answered:
        return None
    return sum(a.answer == "yes" for a in answered) / len(answered)


def _majority(asks: list[list[QuestionResult]]) -> list[QuestionResult]:
    combined = []
    for position, first in enumerate(asks[0]):
        votes = [ask[position] for ask in asks]
        counts = Counter(v.answer for v in votes).most_common()
        top, count = counts[0]
        if len(counts) > 1 and counts[1][1] == count:
            top = "cant_tell"
        chosen = next((v for v in votes if v.answer == top), first)
        combined.append(replace(chosen, answer=top, confidence=count / len(votes)))
    return combined


async def run_checklist(
    run: RunView, params: ChecklistParams, threshold: float, ask: Ask
) -> JudgeResult:
    transcript = Transcript.of(run)
    messages = checklist_messages(transcript, params.questions, params.examples)
    raw, model_version = await _ask_parsed(ask, messages, parse_answers)
    first = _checked(transcript, params.questions, raw)
    score = _score(first)
    if score is None or abs(score - threshold) > BORDERLINE_MARGIN:
        return JudgeResult(first, model_version)
    asks = [first]
    for _ in range(ASKS_WHEN_BORDERLINE - 1):
        raw, model_version = await _ask_parsed(ask, messages, parse_answers)
        asks.append(_checked(transcript, params.questions, raw))
    return JudgeResult(_majority(asks), model_version, borderline=True)


async def run_claim_check(run: RunView, params: ClaimCheckParams, ask: Ask) -> JudgeResult:
    transcript = Transcript.of(run)
    raw, model_version = await _ask_parsed(
        ask, claim_messages(transcript, params.examples), parse_claims
    )
    claims = []
    for text, verdict, quote in raw:
        evidence = locate(quote, transcript, tool_results_only=True) if quote else ()
        # Support or contradiction must be shown by a tool result, quoted exactly.
        if verdict != "unverified" and not evidence:
            verdict, quote = "unverified", None
        claims.append(Claim(text, verdict, quote, evidence))  # type: ignore[arg-type]
    answer: Answer = (
        "cant_tell"
        if not claims
        else "yes"
        if all(c.verdict == "supported" for c in claims)
        else "no"
    )
    evidence = tuple(dict.fromkeys(e for c in claims for e in c.evidence))
    summary = QuestionResult(CLAIMS_QUESTION, answer, None, evidence)
    return JudgeResult([summary], model_version, claims=claims)


def outcome(result: JudgeResult, trusted: set[str]) -> Outcome | None:
    """The judge's score from trusted questions only. None when there is nothing to count:
    no question is trusted yet, or the judge could not tell any of them."""
    if result.claims is not None:
        if CLAIMS_QUESTION.id not in trusted or not result.claims:
            return None
        supported = [c for c in result.claims if c.verdict == "supported"]
        others = [f"{c.verdict}: {c.text[:120]}" for c in result.claims if c not in supported]
        reason = f"{len(supported)} of {len(result.claims)} claims supported"
        if others:
            reason += "; " + "; ".join(others[:5])
        return Outcome(len(supported) / len(result.claims), reason, result.answers[0].evidence)
    counted = [a for a in result.answers if a.question.id in trusted and a.answer != "cant_tell"]
    if not counted:
        return None
    failed = [a.question.id for a in counted if a.answer == "no"]
    reason = f"{len(counted) - len(failed)} of {len(counted)} questions yes" + (
        f"; failed: {', '.join(failed)}" if failed else ""
    )
    confidences = [a.confidence for a in counted if a.confidence is not None]
    return Outcome(
        value=sum(a.answer == "yes" for a in counted) / len(counted),
        reason=reason,
        evidence=tuple(dict.fromkeys(e for a in counted for e in a.evidence)),
        confidence=sum(confidences) / len(confidences) if confidences else None,
    )

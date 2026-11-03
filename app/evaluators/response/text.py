import re

from app.evaluators.contract import Score
from app.evaluators.scores import Named, not_applicable, verdict
from app.evaluators.spec_parts import Claim, ResponseSpec

# Reading a reply clause by clause, so any environment can check what it claims.

# Words up to the end of a claim that turn it into a negation, a condition or a plan.
# "After" and "when" are left out: "after checking, I have refunded you" is still a claim, and
# a real condition comes with a future tense the claim patterns never match.
UNDOING = re.compile(
    r"\b(?:not|no|never|cannot|unable|once|if|until|unless|pending|awaiting|will|would)\b"
    r"|n't\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"(?<=[!?])\s+|(?<=\.)\s+|\n+")
_CLAUSE_BREAK = re.compile(r";|,?\s+but\s+|,\s+(?:and|so|while|however)\s+|\s+however,?\s+")


def clauses(text: str) -> list[str]:
    # Curly apostrophes are normalized so "I\u2019ve" reads like "I've".
    text = text.replace("\u2019", "'")
    out: list[str] = []
    for sentence in _SENTENCE_END.split(text):
        out.extend(c.strip() for c in _CLAUSE_BREAK.split(sentence) if c.strip())
    return out


def states(claim: Claim, text: str) -> bool:
    """Whether the reply asserts the claim, not just mentions it in a negation or condition."""
    if not claim.negatable:
        return bool(claim.found_in(text))
    for clause in clauses(text):
        for pattern in claim.any_of:
            match = re.search(pattern, clause, re.IGNORECASE)
            if match and not UNDOING.search(clause[: match.end()]):
                return True
    return False


def facts_score(evaluator: Named, spec: ResponseSpec, text: str, finish: list[str]) -> Score:
    if not spec.must_include and not spec.must_not_claim:
        return not_applicable(evaluator, "facts")
    false = [c for c in spec.must_not_claim if states(c, text)]
    missing = [p.name for p in spec.must_include if not p.found_in(text)]
    if not false and not missing:
        return verdict(evaluator, "facts", "pass")
    # A false statement is worse than a missing one, so it names the failure.
    reason = false[0].code if false else "MISSING_FACT"
    return verdict(
        evaluator,
        "facts",
        "fail",
        reason=reason,
        evidence={
            "false_claims": [c.name for c in false],
            "missing_facts": missing,
            "event_ids": finish,
        },
    )

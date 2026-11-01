import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.environments.contract import TaskVersion
from app.evaluators.contract import EvalContext, Layer, Score
from app.evaluators.retail_spec import ResponseSpec, RetailFacts, is_retail
from app.evaluators.scores import not_applicable, verdict

# A clause that says a refund happened. Patterns stay in the past or perfect tense, so a
# promise ("I will refund you once...") is never read as a claim.
_CLAIMS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:i|we)(?:'ve| have)?\s+(?:\w+\s+){0,4}?(?:issued|processed|refunded|initiated|"
        r"completed)\b",
        r"\brefunds?\b.{0,40}?\b(?:has|have|had)\s+(?:\w+\s+)?been\s+(?:issued|processed|"
        r"completed|sent|initiated|approved and issued)\b",
        r"\b(?:has|have|had|was|were)\s+(?:\w+\s+)?been\s+refunded\b",
        r"\b(?:was|were)\s+(?:\w+\s+)?refunded\b",
        r"\brefunds?\s+(?:is|was)\s+(?:now\s+)?(?:complete|completed|issued|processed|"
        r"on (?:its|the) way)\b",
    )
]
# A clause that says no refund happened or can happen.
_DENIALS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:unable|not able)\s+to\s+(?:issue|process|complete|refund)\b",
        r"\b(?:cannot|can't|could ?not|couldn't|won't be able to)\s+(?:issue|process|complete|"
        r"refund|give)\b",
        r"\bnot\s+eligible\b",
        r"\bno\s+refund\s+(?:has been|was|can be|will be|is)\b",
        r"\b(?:haven't|have not|hasn't|has not|wasn't|was not)\s+(?:yet\s+)?(?:been\s+)?"
        r"(?:issued|processed|refunded)\b",
        r"\brefund\s+(?:failed|did not go through|didn't go through)\b",
    )
]
# Words up to the end of a claim that turn it into a negation, a condition or a plan.
# "After" and "when" are left out: "after checking, I have refunded you" is still a claim, and
# a real condition comes with a future tense the claim patterns never match.
_UNDOING = re.compile(
    r"\b(?:not|no|never|cannot|unable|once|if|until|unless|pending|awaiting|will|would)\b"
    r"|n't\b",
    re.IGNORECASE,
)
# A claim about an earlier refund, not one made in this conversation.
_HISTORICAL = re.compile(
    r"\b(?:already|previously|earlier|last (?:week|month)|on \d{4}-\d{2}-\d{2}|"
    r"on (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*)\b",
    re.IGNORECASE,
)
_MONEY = re.compile(
    r"(?:[$₹€£]\s?|\b(?:USD|INR|EUR|GBP|Rs\.?)\s?)(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"|(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s?(?:USD|INR|EUR|GBP|dollars|rupees)\b",
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


def _matches(patterns: list[re.Pattern[str]], clause: str) -> bool:
    if "refund" not in clause.lower():
        return False
    for pattern in patterns:
        match = pattern.search(clause)
        if match and (patterns is _DENIALS or not _UNDOING.search(clause[: match.end()])):
            return True
    return False


def claims(text: str) -> list[str]:
    return [c for c in clauses(text) if _matches(_CLAIMS, c)]


def denials(text: str) -> list[str]:
    return [c for c in clauses(text) if _matches(_DENIALS, c) and not _matches(_CLAIMS, c)]


def amounts_minor(text: str) -> set[int]:
    found = set()
    for match in _MONEY.finditer(text):
        raw = (match.group(1) or match.group(2)).replace(",", "")
        try:
            found.add(int(Decimal(raw) * 100))
        except InvalidOperation:
            continue
    return found


class RetailResponse:
    """Checks the final message against what actually happened, not against the agent's say.

    The checks are deterministic patterns, so they catch plain false claims ("refund issued"
    after a rejection) and leave tone and nuance to an optional judge.
    """

    name = "retail_response"
    version = "1.0.0"
    layer: Layer = "response"

    def applies_to(self, task: TaskVersion) -> bool:
        return is_retail(task)

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        facts = RetailFacts(ctx)
        text = ctx.final_response
        finish = [i for i in [ctx.finish_event_id()] if i]
        if not text.strip():
            return [
                verdict(
                    self,
                    "final_response",
                    "fail",
                    reason="NO_FINAL_RESPONSE",
                    evidence={"event_ids": finish},
                ),
                not_applicable(self, "refund_claim"),
                not_applicable(self, "amount_claim"),
                not_applicable(self, "facts"),
            ]
        created = facts.new_refunds(ctx.final_snapshot)
        any_refund = bool(ctx.final_snapshot.get("refunds"))
        refund_events = [
            e.id
            for r in created
            if (e := ctx.tool_result_event(r.get("tool_call_id") or "")) is not None
        ]
        return [
            verdict(self, "final_response", "pass"),
            self._refund_claim(text, created, any_refund, finish, refund_events),
            self._amount_claim(text, created, finish),
            self._facts(facts.spec.response, text, finish),
        ]

    def _refund_claim(
        self,
        text: str,
        created: list[dict[str, Any]],
        any_refund: bool,
        finish: list[str],
        refund_events: list[str],
    ) -> Score:
        made = claims(text)
        false_claims = [c for c in made if not (any_refund if _HISTORICAL.search(c) else created)]
        if false_claims:
            return verdict(
                self,
                "refund_claim",
                "fail",
                reason="FALSE_REFUND_CLAIM",
                evidence={"claims": false_claims, "refunds_created": 0, "event_ids": finish},
            )
        denied = denials(text)
        if created and denied and not made:
            return verdict(
                self,
                "refund_claim",
                "fail",
                reason="REFUND_DENIED_BUT_ISSUED",
                evidence={
                    "denials": denied,
                    "refunds_created": len(created),
                    "event_ids": finish + refund_events,
                },
            )
        return verdict(self, "refund_claim", "pass")

    def _amount_claim(self, text: str, created: list[dict[str, Any]], finish: list[str]) -> Score:
        claimed = set().union(*(amounts_minor(c) for c in claims(text))) if created else set()
        if not claimed:
            return not_applicable(self, "amount_claim")
        actual = {r["amount_minor"] for r in created} | {sum(r["amount_minor"] for r in created)}
        if claimed & actual:
            return verdict(self, "amount_claim", "pass")
        return verdict(
            self,
            "amount_claim",
            "fail",
            reason="WRONG_AMOUNT_CLAIMED",
            evidence={
                "claimed_minor": sorted(claimed),
                "actual_minor": sorted(actual),
                "event_ids": finish,
            },
        )

    def _facts(self, spec: ResponseSpec, text: str, finish: list[str]) -> Score:
        if not spec.must_include and not spec.must_not_claim:
            return not_applicable(self, "facts")
        false = [c for c in spec.must_not_claim if c.found_in(text)]
        missing = [p.name for p in spec.must_include if not p.found_in(text)]
        if not false and not missing:
            return verdict(self, "facts", "pass")
        # A false statement is worse than a missing one, so it names the failure.
        reason = false[0].code if false else "MISSING_FACT"
        return verdict(
            self,
            "facts",
            "fail",
            reason=reason,
            evidence={
                "false_claims": [c.name for c in false],
                "missing_facts": missing,
                "event_ids": finish,
            },
        )

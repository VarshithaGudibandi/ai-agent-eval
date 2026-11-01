from typing import Any, Protocol

from app.evaluators.contract import Layer, Score, ScoreStatus


class Named(Protocol):
    name: str
    version: str
    layer: Layer


_VALUES: dict[str, float | None] = {
    "pass": 1.0,
    "fail": 0.0,
    "partial": None,
    "not_applicable": None,
    "evaluation_error": None,
}


def verdict(
    evaluator: Named,
    dimension: str,
    status: ScoreStatus,
    *,
    reason: str | None = None,
    evidence: dict[str, Any] | None = None,
    value: float | None = None,
) -> Score:
    return Score(
        evaluator=evaluator.name,
        version=evaluator.version,
        layer=evaluator.layer,
        dimension=dimension,
        applicable=status != "not_applicable",
        status=status,
        score=value if value is not None else _VALUES[status],
        reason_code=reason,
        evidence=evidence or {},
    )


def not_applicable(evaluator: Named, dimension: str) -> Score:
    return verdict(evaluator, dimension, "not_applicable")

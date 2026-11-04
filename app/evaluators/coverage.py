from functools import cache
from pathlib import Path

from pydantic import BaseModel

# The published mutation coverage: for each evaluator version, the broken traces it is proven
# to catch. A test rebuilds this table from the mutation suite and fails if the file drifts,
# so it changes whenever an evaluator version does.
COVERAGE_FILE = Path(__file__).with_name("coverage.json")


class Catch(BaseModel):
    mutation: str
    dimension: str
    reason_code: str


class EvaluatorCoverage(BaseModel):
    name: str
    version: str
    layer: str
    catches: list[Catch]


class Coverage(BaseModel):
    evaluators: list[EvaluatorCoverage]
    # Valid ways to do a task that must change no verdict.
    valid_alternatives: list[str]
    # What the grading is known not to catch, said plainly.
    not_covered: list[str]


@cache
def published_coverage() -> Coverage:
    return Coverage.model_validate_json(COVERAGE_FILE.read_text())

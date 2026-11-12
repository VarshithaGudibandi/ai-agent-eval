import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

# A judge question is trusted once enough people have labelled runs it answered, and it
# agreed with them often enough. Until then its answers are kept and shown, not counted.

MIN_LABELS = 10
MIN_AGREEMENT = 0.8


def question_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).casefold().encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Calibration:
    # Labels on runs where the judge gave a yes or no; only these can agree or not.
    labels: int
    agreement: float | None
    # Labelled runs where the judge could not tell, as a share of all labelled runs.
    cant_tell_rate: float | None
    trusted: bool


def calibrate(pairs: Iterable[tuple[str, str]]) -> Calibration:
    """Agreement between the judge's answers and people's, from (judge, human) pairs."""
    pairs = list(pairs)
    answered = [(judge, human) for judge, human in pairs if judge != "cant_tell"]
    agreement = (
        sum(judge == human for judge, human in answered) / len(answered) if answered else None
    )
    return Calibration(
        labels=len(answered),
        agreement=agreement,
        cant_tell_rate=(len(pairs) - len(answered)) / len(pairs) if pairs else None,
        trusted=len(answered) >= MIN_LABELS and (agreement or 0) >= MIN_AGREEMENT,
    )

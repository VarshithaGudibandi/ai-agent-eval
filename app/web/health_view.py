from dataclasses import dataclass
from typing import Literal

from app.metrics.comparison import MetricChange
from app.metrics.health import Health
from app.web.formatting import DASH, duration, money, percent, points

# The six health metrics as people read them, the same on every page.

Tone = Literal["bad", "good", "neutral"]


@dataclass(frozen=True)
class Tile:
    label: str
    value: str
    change: str | None
    # Whether the change is likely real, could be noise, or cannot be told.
    verdict: str | None
    tone: Tone


def _change(metric: MetricChange) -> str | None:
    if metric.delta is None:
        return None
    if metric.name in ("success_rate", "rule_violation_rate", "consistency"):
        return points(metric.delta)
    if metric.name == "duration_p50_ms":
        sign = "+" if metric.delta >= 0 else "-"
        return f"{sign}{duration(abs(metric.delta))}"
    if metric.baseline:
        return f"{metric.delta / metric.baseline * 100:+.0f}%"
    return None


def _verdict(metric: MetricChange) -> str | None:
    if metric.delta is None or metric.delta == 0:
        return None
    if metric.real is None:
        return "too few runs to tell"
    return "likely real" if metric.real else "could be noise"


def _tone(metric: MetricChange | None) -> Tone:
    if metric is None or not metric.real or not metric.delta:
        return "neutral"
    return "bad" if metric.worse else "good"


def tiles(current: Health, metrics: list[MetricChange]) -> list[Tile]:
    by_name = {m.name: m for m in metrics}

    def tile(label: str, value: str, name: str | None) -> Tile:
        metric = by_name.get(name) if name else None
        return Tile(
            label=label,
            value=value,
            change=_change(metric) if metric else None,
            verdict=_verdict(metric) if metric else None,
            tone=_tone(metric),
        )

    run_time = (
        DASH
        if current.duration_p50_ms is None
        else f"{duration(current.duration_p50_ms)} / {duration(current.duration_p95_ms)}"
    )
    return [
        tile("Success rate", percent(current.success_rate), "success_rate"),
        tile("Rule violations", percent(current.rule_violation_rate), "rule_violation_rate"),
        tile("Consistency · passed every repetition", percent(current.consistency), "consistency"),
        tile("Run time · typical / slowest", run_time, "duration_p50_ms"),
        tile(
            "Cost per successful run",
            money(current.cost_per_success_micros)
            + (
                ""
                if current.cost_exact or current.cost_per_success_micros is None
                else " (some runs unpriced)"
            ),
            "cost_per_success_micros",
        ),
        tile("Runs scored", f"{current.runs_scored} of {current.runs}", None),
    ]


@dataclass(frozen=True)
class MetricRow:
    label: str
    baseline: str
    candidate: str
    change: str | None
    verdict: str | None
    tone: Tone


LABELS = {
    "success_rate": "Success rate",
    "rule_violation_rate": "Rule violations",
    "consistency": "Consistency",
    "duration_p50_ms": "Run time, typical",
    "cost_per_success_micros": "Cost per successful run",
}


def _value(name: str, value: float | None) -> str:
    if name == "duration_p50_ms":
        return duration(value)
    if name == "cost_per_success_micros":
        return money(value)
    return percent(value)


def metric_rows(
    baseline: Health, candidate: Health, metrics: list[MetricChange]
) -> list[MetricRow]:
    """The six metrics side by side: A, B, the change, and whether it is real."""
    rows = [
        MetricRow(
            label=LABELS.get(m.name, m.name),
            baseline=_value(m.name, m.baseline),
            candidate=_value(m.name, m.candidate),
            change=_change(m),
            verdict=_verdict(m),
            tone=_tone(m),
        )
        for m in metrics
    ]
    rows.append(
        MetricRow(
            label="Runs scored",
            baseline=f"{baseline.runs_scored} of {baseline.runs}",
            candidate=f"{candidate.runs_scored} of {candidate.runs}",
            change=None,
            verdict=None,
            tone="neutral",
        )
    )
    return rows

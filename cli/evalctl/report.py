from typing import Any

# How a comparison reads in a terminal or a CI log.

LABELS = {
    "success_rate": ("success rate", "pct"),
    "rule_violation_rate": ("rule violations", "pct"),
    "consistency": ("consistency", "pct"),
    "duration_p50_ms": ("run time, typical", "ms"),
    "cost_per_success_micros": ("cost per success", "micros"),
}


def _value(value: float | None, unit: str) -> str:
    if value is None:
        return "-"
    if unit == "pct":
        return f"{value * 100:.1f}%"
    if unit == "ms":
        return f"{value:.0f} ms"
    return f"{value / 1_000_000:.6f}"


def _change(metric: dict[str, Any], unit: str) -> str:
    delta = metric["delta"]
    if delta is None:
        return "-"
    text = f"{delta * 100:+.1f} pts" if unit == "pct" else f"{delta:+.0f}"
    if metric["real"] is None:
        return f"{text} (too few runs to tell)"
    return f"{text} ({'real' if metric['real'] else 'noise'})"


def comparison(result: dict[str, Any], link: str) -> str:
    lines = [f"Decision: {result['decision'].upper()}"]
    lines += [f"  {reason}" for reason in result["reasons"]]
    lines += [f"  note: {note}" for note in result["fairness"]]
    lines.append("")
    lines.append(f"{'metric':<20}{'baseline':>12}{'candidate':>12}  change")
    for metric in result["metrics"]:
        label, unit = LABELS.get(metric["name"], (metric["name"], ""))
        lines.append(
            f"{label:<20}{_value(metric['baseline'], unit):>12}"
            f"{_value(metric['candidate'], unit):>12}  {_change(metric, unit)}"
        )
    worse = [c for c in result["cases"] if c["outcome"] == "worse"]
    if worse:
        lines.append("")
        lines.append(f"Cases that got worse ({len(worse)}):")
        for case in worse[:20]:
            lines.append(
                f"  {case['case_key']}: passed {case['baseline_pass_rate'] * 100:.0f}%"
                f" -> {case['candidate_pass_rate'] * 100:.0f}%"
            )
    lines.append("")
    lines.append(link)
    return "\n".join(lines)

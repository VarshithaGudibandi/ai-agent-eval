from datetime import UTC, datetime

from agent_eval_mcp.answers import QUOTE, ago, explain_error, metrics, quote

from agent_eval.client import PlatformError

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)


def test_times_read_the_way_people_say_them() -> None:
    assert ago("2026-11-13T11:59:30+00:00", NOW) == "just now"
    assert ago("2026-11-13T11:58:00+00:00", NOW) == "2 minutes ago"
    assert ago("2026-11-13T11:00:00+00:00", NOW) == "1 hour ago"
    assert ago("2026-11-10T12:00:00+00:00", NOW) == "3 days ago"


def test_recorded_text_is_cut_short() -> None:
    long = "x" * (QUOTE + 50)
    cut = quote(long)
    assert cut is not None and len(cut) == QUOTE and cut.endswith("…")
    assert (quote("short"), quote(None)) == ("short", None)


def test_metrics_say_how_sure_the_change_is() -> None:
    rows = [
        {"name": "success_rate", "candidate": 0.5, "baseline": 1.0, "delta": -0.5, "real": True},
        {
            "name": "duration_p50_ms",
            "candidate": 900,
            "baseline": 1000,
            "delta": -100,
            "real": None,
        },
        {"name": "consistency", "candidate": 1.0, "baseline": 1.0, "delta": 0.0, "real": False},
        {
            "name": "cost_per_success_micros",
            "candidate": None,
            "baseline": None,
            "delta": None,
            "real": None,
        },
    ]
    found = {m.name: m for m in metrics(rows)}
    assert found["success rate"].verdict == "real" and found["success rate"].change == "-50.0 pts"
    assert found["run time, typical"].verdict == "too few runs to tell"
    assert found["consistency"].verdict is None
    assert found["cost per successful run"].now == "unknown"


def test_platform_errors_become_advice_without_the_token() -> None:
    url = "http://127.0.0.1:8000"
    assert "viewer token" in str(explain_error(PlatformError("no", 403), url))
    assert "Not found" in str(explain_error(PlatformError("no", 404), url))
    assert url in str(explain_error(PlatformError("down"), url))

from datetime import UTC, datetime

from agent_eval.client import PlatformError
from agent_eval_mcp.answers import QUOTE, ago, explain_error, metrics, quote, report_line

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


def test_a_finished_report_gives_each_cause_with_links_to_its_steps() -> None:
    report = {
        "id": "rep_1",
        "status": "done",
        "error": None,
        "report": {
            "headline": "refund_after_lookup now fails in 8 runs, against 0 before",
            "limits": ["This links a change to the failures that followed it."],
            "groups": [
                {
                    "scorer_key": "refund_after_lookup",
                    "question_id": None,
                    "count": 8,
                    "baseline_count": 0,
                    "cause": "The prompt no longer says to look the order up first.",
                    "confidence": "high",
                    "next_step": "Restore the sentence.",
                    "error": None,
                    "claims": [{"text": "c", "spans": [{"run_id": "run_1", "span_id": "s3"}]}],
                }
            ],
        },
    }
    line = report_line(report, "http://127.0.0.1:8000")
    [cause] = line.causes
    assert (cause.confidence, cause.runs_now, cause.runs_before) == ("high", 8, 0)
    assert cause.steps == ["http://127.0.0.1:8000/runs/run_1?span=s3"]
    queued = report_line({"id": "rep_2", "status": "queued", "error": None, "report": None}, "u")
    assert (queued.headline, queued.causes) == (None, [])

from datetime import UTC, datetime

from app.scoring.judge_schedules import next_occurrence

MONDAY_10 = datetime(2026, 11, 16, 10, 30, tzinfo=UTC)


def test_the_next_time_a_schedule_is_due() -> None:
    # Daily at 09:00: already past today, so tomorrow.
    assert next_occurrence(MONDAY_10, None, 9) == datetime(2026, 11, 17, 9, tzinfo=UTC)
    # Daily at 11:00: later today.
    assert next_occurrence(MONDAY_10, None, 11) == datetime(2026, 11, 16, 11, tzinfo=UTC)
    # Mondays at 09:00, asked on a Monday after nine: next Monday.
    assert next_occurrence(MONDAY_10, 0, 9) == datetime(2026, 11, 23, 9, tzinfo=UTC)
    # Fridays at 18:00.
    assert next_occurrence(MONDAY_10, 4, 18) == datetime(2026, 11, 20, 18, tzinfo=UTC)

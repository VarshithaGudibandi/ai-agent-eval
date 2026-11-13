from collections.abc import Callable

import httpx

from app.metrics.health import Health
from app.web.rendering import TEMPLATES
from tests.api.conftest import SignIn


async def test_an_unknown_page_is_a_page_and_an_unknown_api_path_stays_json(
    new_owner: SignIn, clients: Callable[[], httpx.AsyncClient]
) -> None:
    owner = await new_owner()
    page = await owner.get("/no-such-page")
    assert page.status_code == 404 and "Not found" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    api = await owner.get("/api/v1/no-such-route")
    assert (api.status_code, api.json()) == (404, {"detail": "Not Found"})
    missing = await owner.get("/api/v1/runs/run_nope")
    assert (missing.status_code, missing.json()) == (404, {"error": "not_found"})


def test_numbers_built_on_untrusted_judge_questions_carry_a_label() -> None:
    health = Health(
        runs=3,
        runs_scored=3,
        runs_decided=3,
        success_rate=1.0,
        rule_violation_rate=0.0,
        consistency=None,
        duration_p50_ms=1,
        duration_p95_ms=1,
        cost_per_success_micros=None,
        cost_exact=True,
        incomplete=0,
        pending_calibration=True,
    )
    macro = TEMPLATES.env.get_template("parts/macros.html").module.untrusted_note  # type: ignore[attr-defined]
    assert "not trusted yet" in str(macro(health.pending_calibration))
    assert str(macro(False)).strip() == ""

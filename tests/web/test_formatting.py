from types import SimpleNamespace

from app.web.formatting import DASH, duration, money, percent, points, short, version_name


def test_missing_values_are_a_dash_not_a_zero() -> None:
    assert {percent(None), points(None), duration(None), money(None)} == {DASH}


def test_numbers_read_the_way_people_say_them() -> None:
    assert (percent(0.825), points(-0.06), points(0.004)) == ("82%", "-6.0 pts", "+0.4 pts")
    assert (duration(110), duration(6200)) == ("110 ms", "6.2 s")
    assert (money(5100), money(12_500_000)) == ("$0.0051", "$12.50")


def test_a_version_is_named_by_its_label_or_its_fingerprint() -> None:
    assert version_name(SimpleNamespace(label="v7", fingerprint="abc")) == "v7"
    assert version_name(SimpleNamespace(label=None, fingerprint="0123456789ab")) == "01234567…"
    assert short("abc") == "abc"

import pytest

from app.evaluators.response.it_response import _GRANT_CLAIMS, _RESET_CLAIMS, asserted


@pytest.mark.parametrize(
    "text",
    [
        "I've sent a password reset link to your work email.",
        "Your password has been reset.",
        "I have just reset your password.",
        "I sent a password reset link to your email.",
    ],
)
def test_reads_reset_claims(text: str) -> None:
    assert asserted(_RESET_CLAIMS, "password", text)


@pytest.mark.parametrize(
    "text",
    [
        "I can only reset your own password.",
        "I couldn't reset your password because the account is suspended.",
        "Once you confirm, I will reset your password.",
        "I've sent your ticket to security about the password.",
        "I sent your ticket to security about the password.",
    ],
)
def test_ignores_non_reset_claims(text: str) -> None:
    assert not asserted(_RESET_CLAIMS, "password", text)


def test_grant_claims_and_their_negations() -> None:
    assert asserted(_GRANT_CLAIMS, "access", "I've granted you access to the wiki.")
    assert asserted(_GRANT_CLAIMS, "access", "You now have access to github.")
    assert not asserted(_GRANT_CLAIMS, "access", "I can only grant access for your reports.")
    assert not asserted(_GRANT_CLAIMS, "access", "You already have access to jira.")
    assert not asserted(_GRANT_CLAIMS, "access", "I couldn't give you access to payroll.")

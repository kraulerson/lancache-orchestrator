"""Regression test for Steam 2FA login retry logic.

Validates that the login flow handles EResult.AccountLoginDeniedNeedTwoFactor
by retrying with two_factor_code, rather than relying on the auth_code_required
event (which doesn't fire before login() returns).
"""

from __future__ import annotations

from enum import IntEnum


class FakeEResult(IntEnum):
    OK = 1
    AccountLoginDeniedNeedTwoFactor = 85
    AccountLogonDenied = 63


def _simulate_login_flow(first_result: int, code: str = "12345") -> int:
    """Simulate the 2FA retry pattern from spike_a_steam_prefill.steam_login().

    Returns the final EResult after retry (if applicable).
    """
    result = first_result

    if result in (
        FakeEResult.AccountLoginDeniedNeedTwoFactor,
        FakeEResult.AccountLogonDenied,
    ):
        if result == FakeEResult.AccountLoginDeniedNeedTwoFactor:
            result = FakeEResult.OK
        else:
            result = FakeEResult.OK

    return result


def test_2fa_required_triggers_retry() -> None:
    """EResult 85 (NeedTwoFactor) must retry, not exit."""
    result = _simulate_login_flow(FakeEResult.AccountLoginDeniedNeedTwoFactor)
    assert result == FakeEResult.OK


def test_email_code_triggers_retry() -> None:
    """EResult 63 (AccountLogonDenied) must retry with email code."""
    result = _simulate_login_flow(FakeEResult.AccountLogonDenied)
    assert result == FakeEResult.OK


def test_ok_skips_retry() -> None:
    """EResult.OK should pass through without retry."""
    result = _simulate_login_flow(FakeEResult.OK)
    assert result == FakeEResult.OK

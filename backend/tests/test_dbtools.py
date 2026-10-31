"""The test database is required in CI: a missing URL fails there instead of skipping."""

import pytest

from dbtools import unavailable


def test_a_missing_test_database_fails_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="must run in CI"):
        unavailable("OWNVITALS_TEST_DATABASE_URL is not set")


@pytest.mark.parametrize("ci", [None, "", "false"])
def test_a_missing_test_database_skips_outside_ci(
    monkeypatch: pytest.MonkeyPatch, ci: str | None
) -> None:
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)
    with pytest.raises(pytest.skip.Exception, match="not set"):
        unavailable("OWNVITALS_TEST_DATABASE_URL is not set")


def test_the_database_fixture_returns_the_configured_url(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.setenv("OWNVITALS_TEST_DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    assert request.getfixturevalue("database_url") == "postgresql+psycopg://u:p@h/db"


def test_the_database_fixture_fails_in_ci_without_a_url(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.delenv("OWNVITALS_TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="must run in CI"):
        request.getfixturevalue("database_url")


def test_the_database_fixture_skips_locally_without_a_url(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.delenv("OWNVITALS_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception, match="not set"):
        request.getfixturevalue("database_url")

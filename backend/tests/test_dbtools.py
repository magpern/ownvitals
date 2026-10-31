"""The test database is required in CI: a missing URL fails there instead of skipping. And it
must be on this machine, whatever libpq would otherwise pick."""

import os

import pytest

from dbtools import LIBPQ_HOST_ENVIRONMENT, require_loopback_database, unavailable


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
    url = "postgresql+psycopg://u:p@127.0.0.1:5432/db"
    monkeypatch.setenv("OWNVITALS_TEST_DATABASE_URL", url)
    assert request.getfixturevalue("database_url") == url


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


# --- the test database must be on this machine -------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://u:p@127.0.0.1:5432/db",
        "postgresql+psycopg://u:p@127.1.2.3/db",
        "postgresql+psycopg://u:p@localhost/db",
        "postgresql+psycopg://u:p@[::1]:5432/db",
        "postgresql+psycopg://u:p@/db?host=/var/run/postgresql",
        "postgresql+psycopg://u:p@/db?host=127.0.0.1",
        "postgresql+psycopg://u:p@/db?hostaddr=127.0.0.1",
        "postgresql+psycopg://u:p@127.0.0.1,localhost/db",
        "postgresql+psycopg://u:p@127.0.0.1/db?hostaddr=127.0.0.1",
    ],
)
def test_a_loopback_or_local_socket_test_database_is_accepted(url: str) -> None:
    require_loopback_database(url)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://u:p@db.example.com:5432/db",
        "postgresql+psycopg://u:p@10.0.0.5/db",
        "postgresql+psycopg://u:p@192.0.2.1/db",
        "postgresql+psycopg://u:p@[2001:db8::1]/db",
        "postgresql+psycopg://u:p@127.0.0.1,db.example.com/db",
        "postgresql+psycopg://u:p@127.0.0.1/db?host=db.example.com",
        "postgresql+psycopg://u:p@127.0.0.1/db?hostaddr=192.0.2.1",
        "postgresql+psycopg://u:p@localhost.example.com/db",
    ],
)
def test_a_remote_test_database_is_refused_without_echoing_the_password(url: str) -> None:
    with pytest.raises(pytest.fail.Exception, match="loopback host") as excinfo:
        require_loopback_database(url)
    assert ":p@" not in str(excinfo.value)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://u:p@/db",  # no host: PGHOST or libpq's default decides
        "postgresql+psycopg://u:p@/db?host=",
        "postgresql+psycopg://u:p@127.0.0.1,/db",  # an empty entry in the list
        "postgresql+psycopg://u:p@/db?host=127.0.0.1,",
        "postgresql+psycopg://u:p@/db?hostaddr=",
    ],
)
def test_a_test_database_without_a_host_is_refused(url: str) -> None:
    with pytest.raises(pytest.fail.Exception, match="must name a loopback host") as excinfo:
        require_loopback_database(url)
    assert ":p@" not in str(excinfo.value)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://u:p@127.0.0.1/db?service=remote",  # pg_service.conf can name any host
        "postgresql+psycopg://u:p@localhost/db?sslmode=disable&service=remote",
        "postgresql+psycopg://u:p@/db?service=local",
    ],
)
def test_a_test_database_given_as_a_service_is_refused(url: str) -> None:
    with pytest.raises(pytest.fail.Exception, match="service= entry") as excinfo:
        require_loopback_database(url)
    assert ":p@" not in str(excinfo.value)


def test_the_database_fixture_clears_the_libpq_host_environment(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """libpq would use ``PGHOSTADDR`` instead of the host the loopback check saw, and
    ``PGHOST``/``PGSERVICE`` where the URL leaves them open; the fixture drops all three for the
    test process. ``role_urls`` goes through the same check (``conftest._test_database_url``)."""
    for name in LIBPQ_HOST_ENVIRONMENT:
        monkeypatch.setenv(name, "db.example.com")
    monkeypatch.setenv("OWNVITALS_TEST_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1/db")
    request.getfixturevalue("database_url")
    assert [name for name in LIBPQ_HOST_ENVIRONMENT if name in os.environ] == []


def test_the_database_fixture_refuses_a_remote_url_even_outside_ci(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("OWNVITALS_TEST_DATABASE_URL", "postgresql+psycopg://u:p@db.example.com/db")
    with pytest.raises(pytest.fail.Exception, match="loopback host"):
        request.getfixturevalue("database_url")

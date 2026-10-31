"""In prod a database URL's query string holds only allowlisted libpq parameters (the #35 review,
then #5's review round 1): ``password``, ``passfile``, ``conninfo`` and the like would take the
password from somewhere other than the secret file, ``options`` would set session settings.
``test_config_milestone.py`` covers a password embedded in the URL itself.
"""

import pytest
from pydantic import ValidationError

from ownvitals.config import PROD_QUERY_PARAMETERS, Settings

PROD = {"environment": "prod", "public_base_url": "https://ownvitals.example"}
DATABASE_URL_FIELDS = ("database_url", "migrations_database_url", "maint_database_url")
QUERIES = [
    "password=leaky-secret",
    "passfile=/leaky-secret/pgpass",
    "sslmode=require&password=leaky-secret",
    "PASSWORD=leaky-secret",
    "password=leaky-secret&password=leaky-secret-2",
    "conninfo=password%3Dleaky-secret",
    "dsn=password%3Dleaky-secret",
    "service=leaky-secret",
    "options=-c%20app.user_id%3Dleaky-secret",
    "sslpassword=leaky-secret",
    "sslkey=/leaky-secret/key",
    "host=leaky-secret.example",
    "hostaddr=leaky-secret",
    "SSLMODE=leaky-secret",
    "leaky-secret=1",
]


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize("name", DATABASE_URL_FIELDS)
def test_prod_refuses_a_parameter_outside_the_allowlist(name: str, query: str) -> None:
    url = f"postgresql+psycopg://user@db.example/ownvitals?{query}"
    with pytest.raises(ValidationError) as excinfo:
        Settings(**PROD, **{name: url})  # pyright: ignore[reportArgumentType]
    message = str(excinfo.value)
    assert name in message
    assert "may carry only these query parameters" in message
    assert "leaky-secret" not in message
    assert "db.example" not in message  # the field, never the URL


def test_prod_refuses_a_password_hidden_in_conninfo() -> None:
    # SQLAlchemy's psycopg dialect hands ``conninfo`` to psycopg.connect, which logs in with it.
    url = "postgresql+psycopg://ownvitals_app@db/ownvitals?conninfo=password%3Dleaky-secret"
    with pytest.raises(ValidationError, match="may carry only these query parameters") as err:
        Settings(**PROD, database_url=url)  # pyright: ignore[reportArgumentType]
    assert "leaky-secret" not in str(err.value)


@pytest.mark.parametrize("name", DATABASE_URL_FIELDS)
def test_prod_refuses_such_a_url_from_the_environment_too(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv("OWNVITALS_ENVIRONMENT", "prod")
    monkeypatch.setenv("OWNVITALS_PUBLIC_BASE_URL", "https://ownvitals.example")
    monkeypatch.setenv(f"OWNVITALS_{name.upper()}", "postgresql+psycopg://u@db/x?password=leaky")
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    assert name in str(excinfo.value)
    assert "leaky" not in str(excinfo.value)


def test_the_allowlist_is_the_reviewed_one() -> None:
    assert sorted(PROD_QUERY_PARAMETERS) == [
        "application_name",
        "channel_binding",
        "require_auth",
        "ssl_min_protocol_version",
        "sslcrl",
        "sslcrldir",
        "sslmode",
        "sslrootcert",
        "sslsni",
    ]


@pytest.mark.parametrize("name", DATABASE_URL_FIELDS)
def test_prod_accepts_the_allowlisted_parameters(name: str) -> None:
    url = "postgresql+psycopg://user@db.example/ownvitals?sslmode=verify-full&application_name=x"
    assert Settings(**PROD, **{name: url})  # pyright: ignore[reportArgumentType]
    every = "&".join(f"{key}=x" for key in sorted(PROD_QUERY_PARAMETERS))
    assert Settings(**PROD, **{name: f"postgresql+psycopg://user@db/ownvitals?{every}"})  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("environment", ["dev", "test"])
def test_other_environments_may_pass_a_password_parameter(environment: str) -> None:
    url = "postgresql+psycopg://user@127.0.0.1/ownvitals?password=dev-password"
    assert Settings(environment=environment, database_url=url)  # pyright: ignore[reportArgumentType]

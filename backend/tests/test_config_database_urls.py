"""In prod a database URL carries no password in its query string either (the #35 review, #5):
libpq's ``password`` and ``passfile`` parameters would take the password from somewhere other
than the secret file. ``test_config_milestone.py`` covers a password embedded in the URL itself.
"""

import pytest
from pydantic import ValidationError

from ownvitals.config import Settings

PROD = {"environment": "prod", "public_base_url": "https://ownvitals.example"}
DATABASE_URL_FIELDS = ("database_url", "migrations_database_url", "maint_database_url")
QUERIES = [
    "password=leaky-secret",
    "passfile=/leaky-secret/pgpass",
    "sslmode=require&password=leaky-secret",
    "PASSWORD=leaky-secret",
    "password=leaky-secret&password=leaky-secret-2",
]


@pytest.mark.parametrize("query", QUERIES)
@pytest.mark.parametrize("name", DATABASE_URL_FIELDS)
def test_prod_refuses_a_password_parameter_in_a_database_url(name: str, query: str) -> None:
    url = f"postgresql+psycopg://user@db.example/ownvitals?{query}"
    with pytest.raises(ValidationError) as excinfo:
        Settings(**PROD, **{name: url})  # pyright: ignore[reportArgumentType]
    message = str(excinfo.value)
    assert name in message
    assert "must not carry a password or passfile parameter" in message
    assert "leaky-secret" not in message
    assert "db.example" not in message  # the field, never the URL


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


@pytest.mark.parametrize("name", DATABASE_URL_FIELDS)
def test_prod_accepts_other_parameters(name: str) -> None:
    url = "postgresql+psycopg://user@db.example/ownvitals?sslmode=verify-full&application_name=x"
    assert Settings(**PROD, **{name: url})  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("environment", ["dev", "test"])
def test_other_environments_may_pass_a_password_parameter(environment: str) -> None:
    url = "postgresql+psycopg://user@127.0.0.1/ownvitals?password=dev-password"
    assert Settings(environment=environment, database_url=url)  # pyright: ignore[reportArgumentType]

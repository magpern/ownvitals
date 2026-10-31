"""The password secret files are read by ``10-roles.sql`` and by ``config.py`` the same way."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from dbtools import ROLES_SCRIPT as SCRIPT
from dbtools import unavailable
from ownvitals.config import read_secret_files

OWNER, APP = "owner-pw with space", "app-pw"
# Terminators and look-alikes: exactly one LF is dropped, then exactly one CR; nothing else.
ENDINGS = ["\n", "\r\n", "", "\n\n", "\r\r\n"]
ENDING_IDS = ["lf", "crlf", "none", "lf-lf", "cr-crlf"]
pytestmark = pytest.mark.usefixtures("role_urls")  # skips (or fails in CI) without a database


def password_lines() -> list[str]:
    """The part of the script that reads the two secret files, up to the last ``unset``."""
    lines = SCRIPT.read_text("utf-8").splitlines()
    backslash = chr(92)  # psql meta-commands start with it
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"{backslash}set owner_raw"))
    end = next(i for i, ln in enumerate(lines) if ln.startswith(f"{backslash}unset app_raw"))
    return lines[start : end + 1]


def sql_text(value: str) -> str:
    """``value`` as a SQL expression that needs no quoting (hex of its UTF-8 bytes)."""
    return f"convert_from(decode('{value.encode().hex()}', 'hex'), 'UTF8')"


@pytest.mark.parametrize("ending", ENDINGS, ids=ENDING_IDS)
def test_a_password_file_with_any_line_ending_gives_the_same_password(
    tmp_path: Path, ending: str
) -> None:
    psql = shutil.which("psql")
    if psql is None:
        unavailable("psql is not installed")  # a skip locally, a failure in CI
    (tmp_path / "owner").write_bytes((OWNER + ending).encode())
    (tmp_path / "app").write_bytes((APP + ending).encode())
    # Same reading as config.py, so both agree: one LF, then one CR, nothing else.
    secrets_ = read_secret_files(tmp_path, ("owner", "app"))
    rest = ending.removesuffix(chr(10)).removesuffix(chr(13))
    expected_owner, expected_app = OWNER + rest, APP + rest
    assert secrets_ == {"owner": expected_owner, "app": expected_app}

    script = tmp_path / "check.sql"
    script.write_text(
        "\n".join(password_lines())
        + f"\nSELECT CASE WHEN :'owner_password' = {sql_text(expected_owner)}"
        + f" AND :'app_password' = {sql_text(expected_app)}"
        + " THEN 'same' ELSE 'different' END;\n",
        "utf-8",
    )
    url = make_url(os.environ["OWNVITALS_TEST_DATABASE_URL"])
    env = {
        **os.environ,
        "DATABASE_OWNER_PASSWORD_FILE": str(tmp_path / "owner"),
        "DATABASE_PASSWORD_FILE": str(tmp_path / "app"),
        "PGPASSWORD": url.password or "",
    }
    result = subprocess.run(  # noqa: S603
        [
            psql,
            "-X",
            "-tA",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            url.host or "",
            "-p",
            str(url.port or 5432),
            "-U",
            url.username or "",
            "-d",
            url.database or "",
            "-f",
            str(script),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout.strip()) == (0, "same"), result.stderr

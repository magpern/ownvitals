"""#284: a chain past an allowed name (``psycopg.connect.cursor``) is judged step by step. Before,
only the first name after the module was checked, so anything past it passed both scanners
(guard 5 refuses only dunders). Each step now needs a reviewed dotted name of its own in the
table (``"SMTP._get_socket"``), else the egress guard refuses it."""

import pytest

from egress_classified import TABLE
from egress_scan import scan_source

RUNNER = "ownvitals.db.install_functions"  # the one module psycopg is classified for
MAIL = "ownvitals.mail"
CEREMONIES = "ownvitals.auth.passkey_ceremonies"


@pytest.mark.parametrize(
    "source",
    [
        "import psycopg\npsycopg.connect.cursor",
        "import psycopg\nx = psycopg.connect.cursor",
        "import psycopg\npsycopg.connect.__self__",
        "import psycopg\npsycopg.Error.__subclasses__()",
        "import psycopg\npsycopg.connect.__globals__['socket']",
        "from psycopg import connect\nconnect.cursor",
        "from psycopg import connect as c\nc.__self__.connect('')",
        "import psycopg as p\np.connect.cursor",
        "import psycopg\ngetattr(psycopg.connect, 'cursor')",
        "import psycopg\nfrom functools import partial\npartial(psycopg.connect.cursor)",
        "from psycopg.conninfo import make_conninfo\nmake_conninfo.__wrapped__",
        "x = 'psycopg.connect.cursor'",
    ],
)
def test_a_chain_past_an_allowed_name_is_refused(source: str) -> None:
    found = scan_source(source, RUNNER)
    assert any("past an allowed name of psycopg" in w for _, w in found), found


@pytest.mark.parametrize(
    "source",
    [
        "import psycopg\npsycopg.connect('')",
        "import psycopg\nwith psycopg.connect('') as c:\n    c.cursor()",  # a value, not a chain
        "import psycopg\ntry:\n    pass\nexcept psycopg.Error:\n    pass",
        "from psycopg.conninfo import make_conninfo\nmake_conninfo(host='db')",
    ],
)
def test_the_allowed_names_themselves_still_pass(source: str) -> None:
    assert scan_source(source, RUNNER) == []


def test_a_reviewed_chain_passes_and_a_step_past_it_does_not() -> None:
    assert "SMTP._get_socket" in TABLE["smtplib"].names
    assert scan_source("import smtplib\nsmtplib.SMTP._get_socket", MAIL) == []
    for refused in (
        "import smtplib\nsmtplib.SMTP._get_socket.__wrapped__",
        "import smtplib\nsmtplib.SMTP.connect",
        "import smtplib\nsmtplib.SMTP_SSL._get_socket",
    ):
        found = scan_source(refused, MAIL)
        assert any("past an allowed name of smtplib" in w for _, w in found), refused


def test_an_enum_member_chain_needs_each_step() -> None:
    structs = "from webauthn.helpers.structs import ClientDataType\n"
    assert scan_source(structs + "ClientDataType.WEBAUTHN_GET.value", CEREMONIES) == []
    for chain in ("ClientDataType.WEBAUTHN_GET.value.encode", "ClientDataType.__members__"):
        found = scan_source(structs + chain, CEREMONIES)
        assert any("past an allowed name" in w for _, w in found), chain


def test_every_dotted_name_has_its_parent_in_the_entry() -> None:
    """A dotted name admits one more step past a name the entry already admits."""
    for key, entry in TABLE.items():
        for name in entry.names:
            parent = name.rpartition(".")[0]
            assert not parent or parent in entry.names, (key, name)

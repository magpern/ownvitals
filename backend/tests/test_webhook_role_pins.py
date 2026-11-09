"""The webhook roles' pins (#280): the ingest's EXECUTE on queue_enqueue, the two webhook
roles' attributes, and the month partitions' ACL. Synthetic schema state only.

Each drift is planted as the superuser, must be found by the check that pins it (the worker's
queue check, guard 9 or guard 2), and is repaired afterwards.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from jobtools import (  # noqa: F401 - fixtures
    QUEUE_SCRIPT,
    admin_fixture,
    engine_for,
    install_queue,
    queue_schema_fixture,
    run_script,
)
from ownvitals.db.queue_check import QUEUE_QUERY, queue_problems
from queue_guard import check_queue
from role_guard import check_roles

ENQUEUE = "public.queue_enqueue(uuid, text, text, jsonb, text, integer, uuid)"
EVENTS = "ownvitals_webhook_events"
SECRETS = "ownvitals_webhook_secrets"
WITHINGS_SCRIPT = QUEUE_SCRIPT.parent.parent / "withings" / "40-withings.sql"


@pytest.fixture
def superuser(queue_schema: str) -> Iterator[str]:
    """The admin URL; both scripts run again afterwards, whatever a test planted."""
    yield queue_schema
    install_queue(queue_schema)
    run_script(queue_schema, WITHINGS_SCRIPT.read_text(encoding="utf-8"))


def ingest_may_enqueue(admin: Connection) -> bool:
    return bool(
        admin.execute(
            text("SELECT has_function_privilege(:r, :f, 'EXECUTE')"), {"r": EVENTS, "f": ENQUEUE}
        ).scalar_one()
    )


def all_problems(admin: Connection) -> tuple[list[str], list[str], list[str]]:
    worker = queue_problems(admin.execute(text(QUEUE_QUERY)).one())
    return worker, check_queue(admin), check_roles(admin)


def test_a_lone_queue_script_run_keeps_the_ingest_grant(admin: Connection, superuser: str) -> None:
    """20-queue.sql run on its own (the fixture did, after 40-withings.sql) keeps it."""
    run_script(superuser, QUEUE_SCRIPT.read_text(encoding="utf-8"))
    assert ingest_may_enqueue(admin)
    assert all_problems(admin) == ([], [], [])


def test_every_pin_refuses_an_ingest_without_its_enqueue_grant(
    admin: Connection, superuser: str
) -> None:
    admin.execute(text(f"REVOKE EXECUTE ON FUNCTION {ENQUEUE} FROM {EVENTS}"))
    assert not ingest_may_enqueue(admin)
    worker, guard_9, guard_2 = all_problems(admin)
    assert any("public.queue_enqueue differs" in p for p in worker), worker
    assert any("public.queue_enqueue: ACL must be" in p for p in guard_9), guard_9
    assert any(f"{EVENTS} must have EXECUTE" in p for p in guard_2), guard_2
    install_queue(superuser)
    assert ingest_may_enqueue(admin)
    assert all_problems(admin) == ([], [], [])


def test_the_ingest_role_may_execute_no_other_queue_function(
    admin: Connection, superuser: str
) -> None:
    claim = "public.queue_claim(text, integer, text[])"
    admin.execute(text(f"GRANT EXECUTE ON FUNCTION {claim} TO {EVENTS}"))
    _worker, _guard_9, guard_2 = all_problems(admin)
    assert any(f"{EVENTS} must hold nothing" in p for p in guard_2), guard_2


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        (f"ALTER ROLE {SECRETS} BYPASSRLS", f"{SECRETS} must be NOLOGIN"),
        (f"ALTER ROLE {SECRETS} LOGIN", f"{SECRETS} must be NOLOGIN"),
        (f"ALTER ROLE {SECRETS} INHERIT", f"{SECRETS} must be NOLOGIN"),
        (f"ALTER ROLE {EVENTS} LOGIN", f"{EVENTS} must be NOLOGIN"),
        (f"ALTER ROLE {EVENTS} CREATEROLE", f"{EVENTS} must be NOLOGIN"),
        (f"ALTER ROLE {EVENTS} SET work_mem = '64MB'", f"{EVENTS}: no memberships, settings"),
        (f"GRANT {SECRETS} TO {EVENTS}", f"{SECRETS}: no memberships, settings"),
    ],
)
def test_guard_2_pins_the_webhook_role_attributes(
    admin: Connection, superuser: str, statement: str, expected: str
) -> None:
    assert check_roles(admin) == []
    admin.execute(text(statement))
    found = check_roles(admin)
    assert any(p.startswith(expected) for p in found), found
    # 40-withings.sql (run by the fixture afterwards) restores the attributes.


def test_guard_2_checks_the_month_partition_acl(admin: Connection, superuser: str) -> None:
    """A partition made by ensure_month_partitions passes; a grant on it does not."""
    admin.execute(
        text("SELECT public.ensure_month_partitions('intraday', DATE '2026-06-01', :to)"),
        {"to": "2026-06-01"},
    )
    try:
        assert check_roles(admin) == []
        admin.execute(text("GRANT SELECT ON public.intraday_y2026m06 TO ownvitals_app"))
        found = check_roles(admin)
        assert any(p.startswith("intraday_y2026m06:r: ownvitals_app") for p in found), found
        admin.execute(text("REVOKE ALL ON public.intraday_y2026m06 FROM ownvitals_app"))
        admin.execute(text("GRANT SELECT ON public.intraday_y2026m06 TO PUBLIC"))
        found = check_roles(admin)
        assert "intraday_y2026m06:r: PUBLIC must have no privilege" in found, found
    finally:
        admin.execute(text("DROP TABLE IF EXISTS public.intraday_y2026m06"))

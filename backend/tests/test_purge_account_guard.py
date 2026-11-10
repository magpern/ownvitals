"""The admin delete's purge, ``webhook_events_purge_account(p_owner)`` (#362), takes the owner as
an argument: a deliberate, narrow exception to "the owner is the session setting". The definer
refuses inside a user session; this guard pins where it may be named, so no other code path can
purge another owner's webhook events. Since #314 its one caller is the definer
``admin_account_delete`` (``docker/postgres/maint/70-admin-accounts.sql``), and no backend module
calls it: ``ownvitals.db.queue_expected`` and ``queue_expected_extra`` pin its owner and grants,
``ownvitals.db.queue_check`` picks its ACL once the family is installed,
``ownvitals.db.queue_digests`` pins its body and ``ownvitals.db.tables`` describes the table.
Nothing else under ``src/ownvitals`` names it, and no SQL script but its own, the admission list
and the admin account family's does.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "backend" / "src" / "ownvitals"
SQL = ROOT / "docker" / "postgres"
NAME = "webhook_events_purge_account"
PINS = frozenset(
    {
        "db/queue_expected.py",
        "db/queue_expected_extra.py",
        "db/queue_check.py",
        "db/queue_digests.py",
        "db/tables.py",
    }
)
SCRIPTS = frozenset(
    {"withings/40-withings.sql", "queue/20-queue.sql", "maint/70-admin-accounts.sql"}
)


def naming_files(root: Path, pattern: str) -> set[str]:
    """Files under ``root`` matching ``pattern`` whose text contains the name, in any case."""
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob(pattern)
        if NAME in path.read_text(encoding="utf-8").lower()
    }


def test_only_the_pins_name_the_account_purge_in_the_backend() -> None:
    assert naming_files(SRC, "*.py") == PINS


def test_only_its_script_the_admission_list_and_the_admin_delete_name_it() -> None:
    assert naming_files(SQL, "*.sql") == SCRIPTS


def test_the_admin_delete_calls_it_after_the_checks_and_before_the_account_delete() -> None:
    """In ``admin_account_delete``: once, after the audit-row, admin and address checks, before
    the ``account_deleted`` event and the ``users`` row; and the family's role is its grantee."""
    source = (SQL / "maint" / "70-admin-accounts.sql").read_text(encoding="utf-8")
    body = source.split("CREATE OR REPLACE FUNCTION public.admin_account_delete(", 1)[1]
    body = body.split("$fn$;", 1)[0]
    call = f"PERFORM public.{NAME}(p_target);"
    assert body.count(call) == 1
    assert body.count(NAME) == 1
    assert body.index("RAISE EXCEPTION 'no_audit_row'") < body.index(call)
    assert body.index("RETURN 'target_is_admin'") < body.index(call)
    assert body.index("RETURN 'email_mismatch'") < body.index(call)
    assert body.index(call) < body.index("INSERT INTO public.auth_events")
    assert body.index(call) < body.index("DELETE FROM public.users")
    grant = f"GRANT EXECUTE ON FUNCTION public.{NAME}(uuid) TO ownvitals_admin_accounts;"
    assert source.count(grant) == 1


def test_a_new_caller_is_found(tmp_path: Path) -> None:
    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "admin_accounts.py").write_text(f'"{NAME}"', encoding="utf-8")
    (tmp_path / "rogue.py").write_text('q = "SELECT public.WEBHOOK_EVENTS_PURGE_ACCOUNT(:o)"')
    assert naming_files(tmp_path, "*.py") == {"auth/admin_accounts.py", "rogue.py"}

"""The admin delete's purge, ``webhook_events_purge_account(p_owner)`` (#362), takes the owner as
an argument: a deliberate, narrow exception to "the owner is the session setting". The definer
refuses inside a user session; this guard pins where the backend may name it, so no other code
path can purge another owner's webhook events. ``ownvitals.auth.admin_accounts`` calls it;
``ownvitals.db.queue_expected`` pins its owner and grants and ``ownvitals.db.queue_digests`` its
body; ``ownvitals.db.tables`` describes the table. Nothing else under ``src/ownvitals`` names it.
"""

from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "ownvitals"
NAME = "webhook_events_purge_account"
CALLERS = frozenset({"auth/admin_accounts.py"})
PINS = frozenset({"db/queue_expected.py", "db/queue_digests.py", "db/tables.py"})


def naming_modules(root: Path) -> set[str]:
    """Modules under ``root`` whose text contains the function name, in any letter case."""
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if NAME in path.read_text(encoding="utf-8").lower()
    }


def test_only_the_admin_delete_and_the_pins_name_the_account_purge() -> None:
    assert naming_modules(SRC) == CALLERS | PINS


def test_the_admin_delete_calls_it_before_the_account_delete() -> None:
    """The statement, and its call site in ``delete_account``: after the address check, before
    ``delete_user_account``, with the target's id."""
    source = (SRC / "auth" / "admin_accounts.py").read_text(encoding="utf-8")
    assert f'_PURGE_ACCOUNT_EVENTS = text("SELECT public.{NAME}(:owner)")' in source
    body = source.split("async def delete_account(", 1)[1]
    call = 'await db.execute(_PURGE_ACCOUNT_EVENTS, {"owner": user_id})'
    assert body.count(call) == 1
    assert body.index("raise EmailMismatch()") < body.index(call)
    assert body.index(call) < body.index("await delete_user_account(db, user_id, ip=ip)")


def test_a_new_caller_is_found(tmp_path: Path) -> None:
    (tmp_path / "auth").mkdir()
    (tmp_path / "auth" / "admin_accounts.py").write_text(f'"{NAME}"', encoding="utf-8")
    (tmp_path / "rogue.py").write_text('q = "SELECT public.WEBHOOK_EVENTS_PURGE_ACCOUNT(:o)"')
    assert naming_modules(tmp_path) == {"auth/admin_accounts.py", "rogue.py"}

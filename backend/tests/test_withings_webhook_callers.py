"""The webhook-secret function has one application caller (#24): the rotation module."""

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "ownvitals"
NEEDLE = "webhook_secrets_apply"
# Pins name the function. The only module that calls it is the rotation in webhooks.py.
ALLOWED = frozenset(
    {
        "withings/webhooks.py",
        "db/queue_expected.py",
        "db/queue_digests.py",
    }
)


def test_only_the_rotation_module_calls_the_webhook_secret_function() -> None:
    found = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if NEEDLE in path.read_text(encoding="utf-8")
    )
    assert found == sorted(ALLOWED)

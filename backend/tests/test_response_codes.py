"""Response codes instead of English sentences (ADR 0015 section 6); no database, no network."""

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from ownvitals.jobs.errors import CATEGORIES
from ownvitals.response_codes import (
    CONNECTIONS,
    FAMILIES,
    JOB_FAILURES,
    METHOD_DAILY_SUMMARY,
    REMOVAL_REVOKE_INCOMPLETE,
    VIEWS,
    all_codes,
    is_allowed,
)
from ownvitals.withings.disconnect import REMOVAL

SRC = Path(__file__).resolve().parent.parent / "src" / "ownvitals"
CODES_JSON = Path(__file__).resolve().parents[2] / "frontend" / "src" / "api" / "codes.json"

# The route files of the families whose sites this module lists; auth has its own owner.
ROUTE_FILES = {
    "jobs": ["jobs/routes.py", "jobs/events_routes.py"],
    "connections": ["connections/routes.py"],
}


def assert_response_code(family: str, body: dict[str, Any]) -> None:
    """Fails when a response's ``detail.reason`` is not a code of its family."""
    detail = body.get("detail")
    reason = detail.get("reason") if isinstance(detail, dict) else None
    assert isinstance(reason, str), f"no code in {body!r}"
    assert is_allowed(family, reason), f"unknown {family} response code {reason!r}"


def literal_reasons(relative: str) -> set[str]:
    """The constant ``reason`` values of dict literals and ``_refuse("code")`` calls in a file."""
    found: set[str] = set()
    for node in ast.walk(ast.parse((SRC / relative).read_text())):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "reason"
                    and isinstance(value, ast.Constant)
                ):
                    found.add(str(value.value))
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in {"_refuse", "_unknown"}:
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
    return found


@pytest.mark.parametrize("family", sorted(ROUTE_FILES))
def test_every_reason_a_route_family_emits_is_listed(family: str) -> None:
    emitted = set().union(*(literal_reasons(path) for path in ROUTE_FILES[family]))
    assert emitted, family
    assert emitted <= FAMILIES[family], sorted(emitted - FAMILIES[family])


def test_the_scan_finds_the_codes_it_is_meant_to() -> None:
    assert {"not_found", "unknown_kind"} <= literal_reasons("jobs/routes.py")
    assert "unknown_provider" in literal_reasons("connections/routes.py")


def test_an_unknown_code_fails_and_a_listed_one_passes() -> None:
    assert_response_code("jobs", {"detail": {"reason": "not_cancellable"}})
    for body in (
        {"detail": {"reason": "this_is_new"}},
        {"detail": "Not Found"},
        {"detail": {"reason": "The job could not be cancelled."}},
    ):
        with pytest.raises(AssertionError):
            assert_response_code("jobs", body)
    with pytest.raises(AssertionError):
        assert_response_code("connections", {"detail": {"reason": "not_cancellable"}})


def test_codes_are_stable_words_never_sentences() -> None:
    for family, codes in FAMILIES.items():
        for code in codes:
            assert code.replace("_", "").isalnum(), (family, code)
            assert code == code.lower(), (family, code)


def test_the_job_failure_codes_are_the_categories_of_the_queue() -> None:
    assert set(CATEGORIES) == JOB_FAILURES


def test_the_codes_that_replaced_sentences_are_listed() -> None:
    assert REMOVAL == REMOVAL_REVOKE_INCOMPLETE
    assert REMOVAL in CONNECTIONS
    assert METHOD_DAILY_SUMMARY in VIEWS


def test_the_frontend_code_list_is_this_list() -> None:
    """Regenerate with the one-liner in ``docs/dev/i18n.md`` after adding a code."""
    assert json.loads(CODES_JSON.read_text()) == all_codes()

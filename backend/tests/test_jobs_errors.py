"""Failure categories, retry classes, backoff, the user's sentences and the kind registry (ADR 0005
sections 2 and 6); no database."""

import importlib.util
import random
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import DisconnectionError, OperationalError

from ownvitals.jobs import (
    CATEGORIES,
    KINDS,
    RETRYABLE,
    Complete,
    JobFailure,
    JobKind,
    KindRegistry,
    backoff_seconds,
    failure_of,
)
from ownvitals.jobs.errors import BACKOFF_CAP_SECONDS, RATE_LIMIT_CAP_SECONDS
from ownvitals.jobs.sentences import SENTENCES, failure_sentence


def migration_categories() -> set[str]:
    path = Path(__file__).resolve().parent.parent / "migrations" / "versions" / "0003_jobs.py"
    spec = importlib.util.spec_from_file_location("migration_0003", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {c.strip().strip("'") for c in module.CATEGORIES.split(",")}


def test_the_categories_match_the_migration_check() -> None:
    assert set(CATEGORIES) == migration_categories()
    assert len(CATEGORIES) == len(set(CATEGORIES))
    assert frozenset({"upstream_unavailable", "rate_limited"}) == RETRYABLE


def test_every_category_has_one_plain_sentence() -> None:
    assert set(SENTENCES) == set(CATEGORIES)
    for category, sentence in SENTENCES.items():
        assert sentence.endswith("."), category
        assert failure_sentence("failed", category) == sentence
    assert failure_sentence("failed", "made_up") == SENTENCES["internal"]
    for state in ("queued", "running", "completed", "cancelled"):
        assert failure_sentence(state, None) is None
    assert failure_sentence("failed", None) is None


@pytest.mark.parametrize("attempt", [1, 2, 3, 4, 5, 6, 10])
def test_backoff_doubles_from_30_seconds_with_jitter_up_to_15_minutes(attempt: int) -> None:
    base = min(30 * 2 ** (attempt - 1), BACKOFF_CAP_SECONDS)
    for seed in range(50):
        delay = backoff_seconds(attempt, rng=random.Random(seed))  # noqa: S311
        assert base * 0.8 <= delay <= base * 1.2


def test_a_rate_limit_waits_at_least_its_hint_capped_at_an_hour() -> None:
    assert backoff_seconds(1, 600) >= 600
    assert backoff_seconds(1, 86_400) == RATE_LIMIT_CAP_SECONDS
    assert backoff_seconds(1, 0) <= 36
    assert backoff_seconds(0) <= 36  # an attempt below 1 counts as the first


@pytest.mark.parametrize(
    ("raised", "category", "retry_after"),
    [
        (JobFailure("upstream_unavailable"), "upstream_unavailable", None),
        (JobFailure("rate_limited", retry_after=60), "rate_limited", 60),
        (JobFailure("not_connected", retry_after=60), "not_connected", None),
        (JobFailure("synthetic_unknown"), "internal", None),
        (OperationalError("SELECT 1", {}, ConnectionError()), "upstream_unavailable", None),
        (DisconnectionError(), "upstream_unavailable", None),
        (RuntimeError("synthetic text"), "internal", None),
        (KeyError("synthetic"), "internal", None),
    ],
)
def test_every_exception_maps_to_one_category(
    raised: BaseException, category: str, retry_after: float | None
) -> None:
    failure = failure_of(raised)
    assert (failure.category, failure.retry_after) == (category, retry_after)
    assert failure.args == (category,)  # never the exception's text


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = "fine"


class Loose(BaseModel):
    note: str = "fine"


async def _run(job_run: object) -> Complete:
    return Complete()


def test_the_registry_holds_valid_kinds_once() -> None:
    registry = KindRegistry()
    kind = registry.register(JobKind("test_kind", Strict, _run))
    assert registry.get("test_kind") is kind
    assert registry.get("other") is None
    assert registry.names() == ["test_kind"]
    assert list(registry) == [kind]
    with pytest.raises(ValueError, match="registered already"):
        registry.register(JobKind("test_kind", Strict, _run))
    assert kind.parse_input({"note": "x"}) == Strict(note="x")


@pytest.mark.parametrize(
    ("name", "model", "message"),
    [
        ("Bad-Kind", Strict, "a-z"),
        ("", Strict, "a-z"),
        ("k" * 41, Strict, "a-z"),
        ("test_kind", Loose, "forbid extra"),
    ],
)
def test_invalid_kinds_are_refused(name: str, model: type[BaseModel], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        JobKind(name, model, _run)


def test_the_worker_and_the_api_ship_no_kind_yet() -> None:
    """#9 registers no kind: M1 adds the Withings kinds, each reviewed in its own PR."""
    assert KINDS.names() == []


def test_the_advisory_lock_registry_matches_adr_0005_section_9() -> None:
    """One namespace per row of the ADR's table, by number and name, and no number twice."""
    import re

    from ownvitals.db.queue_locks import NAMESPACES

    adr = Path(__file__).resolve().parents[2] / "docs" / "adr" / "0005-jobs-and-leases.md"
    section = adr.read_text(encoding="utf-8").split("### 9. Advisory-lock namespaces")[1]
    rows = re.findall(r"^\| (\d+), ([a-z -]+?)(?: \(amended [^)]*\))? \|", section, re.MULTILINE)
    assert rows
    documented = {name.replace("-", "_").replace(" ", "_"): int(n) for n, name in rows}
    assert documented == NAMESPACES
    assert len(set(NAMESPACES.values())) == len(NAMESPACES)

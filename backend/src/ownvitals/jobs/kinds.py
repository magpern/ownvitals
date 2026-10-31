"""The job kinds (ADR 0005 section 2): each maps to its input model and its handler.

A kind is registered in a ``KindRegistry``; the worker claims only registered kinds and the jobs
API accepts only registered kinds. #9 ships no real kind (``KINDS`` is empty, so the worker claims
nothing and ``POST /api/jobs`` refuses every kind); the tests register one test-only kind. M1
registers PLAN section 4.4's Withings kinds here.

``run(job_run)`` is the kind's handler for one attempt. It reaches every checkpoint through
``job_run`` (``ownvitals.jobs.run``) and ends with ``Complete(store)``: ``store`` (optional) runs
inside the final fenced transaction, so what it writes and the ``completed`` state commit
together or not at all. It fails a run by raising ``JobFailure(category)``, also from inside
``store`` (a last check in the final transaction): the store is rolled back and the attempt ends
with that category.
"""

import re
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ownvitals.jobs.fence import FencedSession

if TYPE_CHECKING:
    from ownvitals.jobs.run import JobRun

KIND_NAME = re.compile(r"^[a-z_]{1,40}$")  # the jobs.kind check of migration 0003


@dataclass(frozen=True, slots=True)
class Complete:
    """The attempt succeeded: ``store`` runs in the final fenced transaction, then the job is
    ``completed`` (or ``cancelled``, if a cancel was requested meanwhile)."""

    store: Callable[[FencedSession], Awaitable[None]] | None = None


Outcome = Complete


@dataclass(frozen=True, slots=True)
class JobKind:
    name: str
    input_model: type[BaseModel]
    run: "Callable[[JobRun], Awaitable[Outcome]]"

    def __post_init__(self) -> None:
        if not KIND_NAME.match(self.name):
            raise ValueError("a kind name is 1 to 40 of a-z and _")
        if self.input_model.model_config.get("extra") != "forbid":
            raise ValueError("a kind's input model must forbid extra fields")

    def parse_input(self, data: Mapping[str, Any]) -> BaseModel:
        return self.input_model.model_validate(dict(data))


class KindRegistry:
    def __init__(self) -> None:
        self._kinds: dict[str, JobKind] = {}

    def register(self, kind: JobKind) -> JobKind:
        if kind.name in self._kinds:
            raise ValueError("this kind is registered already")
        self._kinds[kind.name] = kind
        return kind

    def get(self, name: str) -> JobKind | None:
        return self._kinds.get(name)

    def names(self) -> list[str]:
        return sorted(self._kinds)

    def __iter__(self) -> Iterator[JobKind]:
        return iter(list(self._kinds.values()))


# The worker's and the API's registry. Empty until M1 registers the Withings kinds.
KINDS = KindRegistry()

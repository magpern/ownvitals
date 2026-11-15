"""What the user chooses for a doctor PDF (ADR 0013 section 6), and the job's stored input.

The choices are a period of at most 12 months, metric keys and at most 10 ECG recording ids.
Nothing else: no name, no email and no free text travels in the request body or in the job's
stored arguments (``extra="forbid"``). The request and the job input share this model.
"""

from datetime import date
from typing import Annotated, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ownvitals.analysis.quality import METRICS

MAX_PERIOD_DAYS: Final = 366  # up to 12 months
MAX_ECG: Final = 10
MAX_METRICS: Final = len(METRICS)


class DoctorChoices(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date
    end: date
    metrics: Annotated[tuple[str, ...], Field(min_length=1, max_length=MAX_METRICS)]
    recordings: Annotated[tuple[UUID, ...], Field(max_length=MAX_ECG)] = ()

    @field_validator("metrics")
    @classmethod
    def _known_metrics(cls, keys: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(keys)) != len(keys) or any(key not in METRICS for key in keys):
            raise ValueError("metrics must be distinct known metric keys")
        return keys

    @field_validator("recordings")
    @classmethod
    def _distinct(cls, ids: tuple[UUID, ...]) -> tuple[UUID, ...]:
        if len(set(ids)) != len(ids):
            raise ValueError("recordings must be distinct")
        return ids

    @model_validator(mode="after")
    def _period(self) -> "DoctorChoices":
        days = (self.end - self.start).days
        if days < 0 or days >= MAX_PERIOD_DAYS:
            raise ValueError("the period is at most 12 months")
        return self


class DoctorInput(DoctorChoices):
    """The job's input: the choices and the export row the job fills."""

    export_id: UUID

"""The base of the Withings answer models (``models.py``): the strict integer and ``Decimal``
types, the spec quirks' validators, the documented enum values and ``WithingsModel``, which keeps
unknown fields in ``raw``."""

from decimal import Decimal
from typing import Annotated, Any, ClassVar, Final

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    Strict,
    model_validator,
)

Int = Annotated[int, Strict()]
Num = Decimal  # the client parses every JSON non-integer as a Decimal

DOCUMENTED_ATTRIB: Final = frozenset({0, 1, 2, 4, 5, 7, 8, 15})  # findings §2
DOCUMENTED_POSITION: Final = frozenset({2, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14})
DOCUMENTED_CATEGORY: Final = frozenset({1, 2})  # 1 real measurement, 2 objective
DOCUMENTED_AFIB: Final = frozenset({0, 1, 2})
DOCUMENTED_AFIB_DETAILS: Final = frozenset({-1, 0, 1, 2, 5, 6, 7, 9, 10, 11, 12, 13})
DOCUMENTED_SLEEP_STATE: Final = frozenset({0, 1, 2, 3, 4, 5, 15})


def _more(value: Any) -> Any:
    """``more``: an integer (``getmeas``) or a boolean (everything else)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    raise ValueError("more must be an integer or a boolean")


def _digits(value: Any) -> Any:
    """An integer that the API sometimes sends as a string of digits."""
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return value


def _timestamp_keys(value: Any) -> Any:
    """A map keyed by epoch seconds as strings (``{"1793404800": 61}``); ``[]`` when empty."""
    if isinstance(value, list) and not value:
        return {}
    if isinstance(value, dict):
        return {_digits(k): v for k, v in value.items()}
    return value


More = Annotated[bool, BeforeValidator(_more)]
IntOrDigits = Annotated[Int, BeforeValidator(_digits)]
Series = Annotated[dict[Int, Num], BeforeValidator(_timestamp_keys)]


class WithingsModel(BaseModel):
    """Every answer model: immutable, unknown fields in ``raw``."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    raw_values: ClassVar[bool] = True  # False: keep only the names of unknown fields

    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _keep_unknown(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        known = set(cls.model_fields) - {"raw"}
        fields = {k: v for k, v in data.items() if k in known}
        unknown = {
            str(k): (v if cls.raw_values else None) for k, v in data.items() if k not in known
        }
        return {**fields, "raw": unknown}

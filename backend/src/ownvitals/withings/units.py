"""Withings' fixed-point values as exact decimals (withings-findings §2: "Real value =
``value * 10^unit``").

This is the one place that turns a Withings ``(value, unit)`` pair into a number. The result is a
``Decimal`` built from the digits themselves, so nothing is rounded and no ``float`` is ever made
(``0.1`` kg stays ``Decimal("0.1")``). Both parts must be real integers (a ``bool`` is refused),
and the exponent is bounded so that a hostile answer cannot ask for a number with a million
digits.
"""

from decimal import Decimal
from typing import Final

# Withings' units are small exponents (-3 for kilograms, -2 for metres); anything outside this
# range is not a measurement.
UNIT_MIN: Final = -30
UNIT_MAX: Final = 30


def _integer(name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    return value


def real_value(value: int, unit: int) -> Decimal:
    """``value * 10^unit`` exactly. ``TypeError`` for a non-integer, ``ValueError`` for an
    exponent outside ``UNIT_MIN``..``UNIT_MAX``."""
    number = _integer("value", value)
    exponent = _integer("unit", unit)
    if not UNIT_MIN <= exponent <= UNIT_MAX:
        raise ValueError("unit is outside the supported range")
    return Decimal(f"{number}E{exponent}")


__all__ = ["UNIT_MAX", "UNIT_MIN", "real_value"]

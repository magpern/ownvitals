"""The grounding grader's number parser (ADR 0014 section 9: "every number in an answer appears in
a tool result of that turn, checked by a parser"; #527). Pure.

``ungrounded(answer, results, prompt)`` lists the numbers of ``answer`` that no tool result of
the turn explains. A number is explained when, rounded to the decimals it is written with, it
equals a number of a result (``72`` for 72.14, ``72.1`` for 72.14), when it is a part of a date
of a result, the length of a list in a result, a number the user's own message (or the digest's
request) already holds, or a plain 0 or 1. ISO dates and clock times in the answer are not
numbers; list numbering ("1.") is not a number. Numbers spelled out in words are not parsed
(a known gap: ``docs/dev/ai.md``).
"""

import re
from collections.abc import Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_TIME = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_LIST_MARK = re.compile(r"(?m)^\s*(?:[-*]\s+)?\d{1,2}[.)]\s")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?(?![\w])")
_DATE_PART = re.compile(r"\d+")


def _decimal(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None


def numbers_in(text: str) -> list[str]:
    """The number tokens of a text, without dates, times and list numbering."""
    cleaned = _LIST_MARK.sub(" ", _TIME.sub(" ", _DATE.sub(" ", text)))
    return _NUMBER.findall(cleaned)


def _walk(value: Any, numbers: set[Decimal], lengths: set[int]) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, int | float):
        numbers.add(Decimal(str(value)))
    elif isinstance(value, str):
        for part in _DATE_PART.findall(value) if _DATE.fullmatch(value) else ():
            numbers.add(Decimal(part))
    elif isinstance(value, Mapping):
        for item in value.values():  # pyright: ignore[reportUnknownVariableType]
            _walk(item, numbers, lengths)
    elif isinstance(value, list | tuple):
        lengths.add(len(value))  # pyright: ignore[reportUnknownArgumentType]
        for item in value:  # pyright: ignore[reportUnknownVariableType]
            _walk(item, numbers, lengths)


def _explained(token: str, numbers: Iterable[Decimal], lengths: set[int]) -> bool:
    shown = _decimal(token)
    if shown is None:
        return True
    places = len(token.split(".")[1]) if "." in token else 0
    quantum = Decimal(1).scaleb(-places)
    if shown in (0, 1) or (shown == shown.to_integral_value() and int(shown) in lengths):
        return True
    return any(number.quantize(quantum, rounding=ROUND_HALF_UP) == shown for number in numbers)


def ungrounded(answer: str, results: Iterable[Mapping[str, Any]], prompt: str) -> list[str]:
    """The numbers of ``answer`` that neither the turn's tool ``results`` nor ``prompt`` explain."""
    numbers: set[Decimal] = set()
    lengths: set[int] = set()
    for result in results:
        _walk(result, numbers, lengths)
    for token in numbers_in(prompt) + [m for m in _DATE_PART.findall(prompt)]:
        parsed = _decimal(token)
        if parsed is not None:
            numbers.add(parsed)
    return [t for t in numbers_in(answer) if not _explained(t, numbers, lengths)]

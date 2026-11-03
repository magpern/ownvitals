"""The pricing table and the per-request upper bound (ADR 0014 section 7).

Prices are integer **micro-USD per million tokens**, per model id, with the date and source of
the price. A model that is not in the table cannot be called (``ModelUnpriced``,
``ai_model_unpriced``); a price change is a reviewed PR. The table is empty until the owner
chooses the model (ADR 0014 open question 3), so no request can be bounded and none is made.

The bound of one request (each step of a tool loop), before it is sent:

- input tokens: the UTF-8 byte length of the serialized request (a token covers at least one
  byte of text) plus a fixed allowance of ``TOOL_ALLOWANCE_TOKENS`` for what the provider adds
  around tools and system text, each at the highest of the input, cache-write and cache-read
  prices (ADR 0014 names the input and cache-write prices; a cache-read price above both would
  otherwise break the bound);
- output tokens: ``max_tokens`` at the output price.

Both the bound and a cost are rounded up once, from the exact sum, so the bound is never below
the cost of a response whose token counts stay within it (``tests/test_ai_pricing.py``).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Final

PER_MILLION: Final = 1_000_000
TOOL_ALLOWANCE_TOKENS: Final = 2_000


@dataclass(frozen=True, slots=True)
class Price:
    """Micro-USD per million tokens, and where the numbers come from."""

    input: int
    output: int
    cache_write: int
    cache_read: int
    as_of: date
    source: str

    def __post_init__(self) -> None:
        for value in (self.input, self.output, self.cache_write, self.cache_read):
            if type(value) is not int or value < 0:
                raise ValueError("a price is a non-negative integer of micro-USD per MTok")


# Model id -> price. Empty until the owner picks the model (ADR 0014 open question 3).
PRICES: Mapping[str, Price] = MappingProxyType({})


class ModelUnpriced(LookupError):
    """The model is not in the pricing table: it must not be called."""

    code: Final = "ai_model_unpriced"


def _price(model: str, prices: Mapping[str, Price]) -> Price:
    price = prices.get(model)
    if price is None:
        raise ModelUnpriced(ModelUnpriced.code)
    return price


def _micro_usd(token_price_products: int) -> int:
    """The sum of ``tokens * price per million`` in micro-USD, rounded up once."""
    return -(-token_price_products // PER_MILLION)


def _count(value: int, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def request_bound(
    model: str, request_bytes: int, max_tokens: int, prices: Mapping[str, Price] = PRICES
) -> int:
    """The most one request can cost, in micro-USD, before it is sent."""
    price = _price(model, prices)
    input_tokens = _count(request_bytes, "request_bytes") + TOOL_ALLOWANCE_TOKENS
    output_tokens = _count(max_tokens, "max_tokens")
    if output_tokens < 1:
        raise ValueError("max_tokens must be positive")
    input_price = max(price.input, price.cache_write, price.cache_read)
    return _micro_usd(input_tokens * input_price + output_tokens * price.output)


def usage_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int = 0,
    cache_read_tokens: int = 0,
    prices: Mapping[str, Price] = PRICES,
) -> int:
    """What a completed request costs, in micro-USD, from the usage the provider reported."""
    price = _price(model, prices)
    return _micro_usd(
        _count(input_tokens, "input_tokens") * price.input
        + _count(output_tokens, "output_tokens") * price.output
        + _count(cache_write_tokens, "cache_write_tokens") * price.cache_write
        + _count(cache_read_tokens, "cache_read_tokens") * price.cache_read
    )

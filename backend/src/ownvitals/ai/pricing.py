"""The pricing table and the per-request upper bound (ADR 0014 section 7).

Prices are integer **micro-USD per million tokens**, per model id, with the date and source of
the price. A model that is not in the table cannot be called (``ModelUnpriced``,
``ai_model_unpriced``); a price change is a reviewed PR. A priced model is still not called
until the owner sets ``ai_model`` and the other locks (ADR 0014 section 1, open question 3).

The bound of one request (each step of a tool loop), before it is sent:

- input tokens: the UTF-8 byte length of the serialized request (a token covers at least one
  byte of text) plus a fixed allowance of ``TOOL_ALLOWANCE_TOKENS`` for what the provider adds
  around tools and system text, each at the highest of the input, cache-write and cache-read
  prices (ADR 0014 names the input and cache-write prices; a cache-read price above both would
  otherwise break the bound);
- output tokens: ``max_tokens`` at the output price.

The table holds the 1x prices of global inference. Every request asks for it
(``"inference_geo": "global"``, ``ai.requests``), so a workspace that does not allow global
inference refuses the request with a 400 instead of running it at the US-only price. A cost is
still computed from where the provider reports the inference ran (``usage.inference_geo``):
anything but ``"global"``, an absent field included, is charged at the US-only multiplier of
1.1x on every category (``GEO_PREMIUM``), so a settlement is never below the bill. The bound
stays at 1x: such an answer can only follow a provider that ignored the request, and it settles
as an overrun (``ai_cost_overrun``), charged in full against the caps.

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
# The inference geo every request asks for, and the one these prices are for.
GLOBAL_GEO: Final = "global"
# Numerator and denominator of the price of any other inference geo (US-only: 1.1x, every
# category; https://platform.claude.com/docs/en/manage-claude/data-residency, 2026-11-17).
GEO_PREMIUM: Final = (11, 10)


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


PRICING_SOURCE: Final = "https://platform.claude.com/docs/en/about-claude/pricing"

# Model id -> price. Pricing a model does not enable it: the owner still sets ``ai_model`` and
# the other locks (ADR 0014 open question 3). The cache-write price is the 5-minute write; the
# client sends no ``cache_control`` (a 1-hour write, $4/MTok, would exceed the bound). These are
# the 1x prices of global inference: the request asks for ``inference_geo: "global"``, and a cost
# reported for any other geo is charged at ``GEO_PREMIUM`` (module docstring).
# No long-context price for this model.
PRICES: Mapping[str, Price] = MappingProxyType(
    {
        "claude-sonnet-5-5": Price(
            input=2_000_000,
            output=10_000_000,
            cache_write=2_500_000,
            cache_read=100_000,
            as_of=date(2026, 11, 15),
            source=PRICING_SOURCE,
        ),
    }
)


class ModelUnpriced(LookupError):
    """The model is not in the pricing table: it must not be called."""

    code: Final = "ai_model_unpriced"


def _price(model: str, prices: Mapping[str, Price]) -> Price:
    price = prices.get(model)
    if price is None:
        raise ModelUnpriced(ModelUnpriced.code)
    return price


def _micro_usd(token_price_products: int, premium: tuple[int, int] = (1, 1)) -> int:
    """The sum of ``tokens * price per million`` in micro-USD, times ``premium``, rounded up
    once."""
    numerator, denominator = premium
    return -(-(token_price_products * numerator) // (PER_MILLION * denominator))


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
    inference_geo: str | None,
    prices: Mapping[str, Price] = PRICES,
) -> int:
    """What a completed request costs, in micro-USD, from the usage the provider reported.

    ``inference_geo`` is the reported ``usage.inference_geo`` (``None`` when absent). It is
    required so that no caller can settle at 1x without passing it: only ``"global"`` is 1x.
    """
    price = _price(model, prices)
    premium = (1, 1) if inference_geo == GLOBAL_GEO else GEO_PREMIUM
    return _micro_usd(
        _count(input_tokens, "input_tokens") * price.input
        + _count(output_tokens, "output_tokens") * price.output
        + _count(cache_write_tokens, "cache_write_tokens") * price.cache_write
        + _count(cache_read_tokens, "cache_read_tokens") * price.cache_read,
        premium,
    )

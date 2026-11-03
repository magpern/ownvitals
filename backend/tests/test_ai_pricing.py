"""The pricing table and the request bound (ADR 0014 section 7, #205).

The table is empty until the owner picks the model, so every model is unpriced. With a
synthetic price (not a real one: the numbers are made up for the test), the bound of a request
is never below the cost of any response whose token counts stay within what the bound assumes:
at most one input token per byte of the request plus the fixed allowance, at most ``max_tokens``
output tokens, cached or not.
"""

import itertools
from datetime import date

import pytest

from ownvitals.ai import pricing
from ownvitals.ai.pricing import ModelUnpriced, Price, request_bound, usage_cost

SYNTHETIC = {
    "synthetic-model": Price(
        input=3_000_000,
        output=15_000_000,
        cache_write=3_750_000,
        cache_read=300_000,
        as_of=date(2026, 11, 3),
        source="synthetic test price",
    ),
    "synthetic-odd": Price(
        input=7, output=11, cache_write=5, cache_read=13, as_of=date(2026, 11, 3), source="test"
    ),
}


def test_no_model_is_priced_until_the_owner_picks_one() -> None:
    assert dict(pricing.PRICES) == {}
    with pytest.raises(ModelUnpriced, match="ai_model_unpriced"):
        request_bound("any-model", 100, 100)
    with pytest.raises(ModelUnpriced):
        usage_cost("any-model", input_tokens=1, output_tokens=1)


def test_an_unknown_model_is_refused_even_with_a_table() -> None:
    with pytest.raises(ModelUnpriced):
        request_bound("synthetic-other", 1, 1, SYNTHETIC)


def test_the_bound_adds_the_allowance_at_the_highest_input_price() -> None:
    # (1000 + 2000) tokens at 3.75 per token, 500 at 15 per token.
    assert request_bound("synthetic-model", 1000, 500, SYNTHETIC) == 3000 * 3 + 3000 * 3 // 4 + 7500


@pytest.mark.parametrize("model", sorted(SYNTHETIC))
def test_the_bound_is_never_below_the_cost_of_a_response_within_it(model: str) -> None:
    for request_bytes, max_tokens in itertools.product((0, 1, 17, 4096), (1, 3, 1024)):
        bound = request_bound(model, request_bytes, max_tokens, SYNTHETIC)
        most_input = request_bytes + pricing.TOOL_ALLOWANCE_TOKENS
        for share in (0, 1, most_input // 3, most_input):
            for cached_read, written in ((0, 0), (share, 0), (0, share)):
                plain = most_input - cached_read - written
                for output in (0, max_tokens // 2, max_tokens):
                    cost = usage_cost(
                        model,
                        input_tokens=plain,
                        output_tokens=output,
                        cache_write_tokens=written,
                        cache_read_tokens=cached_read,
                        prices=SYNTHETIC,
                    )
                    assert cost <= bound, (request_bytes, max_tokens, share, output)


@pytest.mark.parametrize(
    ("request_bytes", "max_tokens"), [(-1, 1), (1, 0), (1, -1), (1.0, 1), (True, 1)]
)
def test_the_bound_refuses_bad_counts(request_bytes: object, max_tokens: object) -> None:
    with pytest.raises(ValueError, match="must be"):
        request_bound("synthetic-model", request_bytes, max_tokens, SYNTHETIC)  # type: ignore[arg-type]


def test_a_price_must_be_a_non_negative_integer() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        Price(input=-1, output=0, cache_write=0, cache_read=0, as_of=date(2026, 1, 1), source="x")
    with pytest.raises(ValueError, match="non-negative"):
        Price(input=1.5, output=0, cache_write=0, cache_read=0, as_of=date(2026, 1, 1), source="x")  # type: ignore[arg-type]

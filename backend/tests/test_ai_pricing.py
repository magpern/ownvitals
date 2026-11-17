"""The pricing table and the request bound (ADR 0014 section 7, #205).

The table prices one model (``claude-sonnet-5-5``, the Architect's proposal; the owner still
sets ``ai_model``); every other model is unpriced. With the real table and with synthetic
prices (made up for the test), the bound of a request is never below the cost of any response
whose token counts stay within what the bound assumes: at most one input token per byte of the
request plus the fixed allowance, at most ``max_tokens`` output tokens, cached or not, with
global inference. A cost reported for any other inference geo, or none, is 1.1x (#529).
"""

import itertools
from collections.abc import Mapping
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


SONNET = "claude-sonnet-5-5"
TABLES: dict[str, Mapping[str, Price]] = {"synthetic": SYNTHETIC, "real": pricing.PRICES}
CASES = [(name, model) for name, table in TABLES.items() for model in sorted(table)]


def test_the_table_prices_only_the_proposed_model() -> None:
    assert sorted(pricing.PRICES) == [SONNET]
    price = pricing.PRICES[SONNET]
    # Anthropic's list prices per MTok: $2 input, $10 output, $2.50 5-minute write, $0.10 read.
    assert (price.input, price.output, price.cache_write, price.cache_read) == (
        2_000_000,
        10_000_000,
        2_500_000,
        100_000,
    )
    assert price.as_of == date(2026, 11, 15)
    assert price.source.startswith("https://platform.claude.com/")


def test_the_table_cannot_be_changed_at_run_time() -> None:
    with pytest.raises(TypeError):
        pricing.PRICES["synthetic-model"] = SYNTHETIC["synthetic-model"]  # type: ignore[index]


def test_an_unknown_model_is_unpriced_in_the_real_table() -> None:
    for model in ("any-model", "claude-opus-5-5", "claude-haiku-5-5", "Claude-Sonnet-5-5"):
        with pytest.raises(ModelUnpriced, match="ai_model_unpriced"):
            request_bound(model, 100, 100)
        with pytest.raises(ModelUnpriced):
            usage_cost(model, input_tokens=1, output_tokens=1, inference_geo="global")


def test_the_real_bound_uses_the_cache_write_price_for_input() -> None:
    # (1000 + 2000) tokens at 2.5 micro-USD each, 500 output tokens at 10 each.
    assert request_bound(SONNET, 1000, 500) == 3000 * 5 // 2 + 5000
    assert usage_cost(SONNET, input_tokens=1_000_000, output_tokens=0, inference_geo="global") == (
        2_000_000
    )


def test_an_unknown_model_is_refused_even_with_a_table() -> None:
    with pytest.raises(ModelUnpriced):
        request_bound("synthetic-other", 1, 1, SYNTHETIC)


def test_the_bound_adds_the_allowance_at_the_highest_input_price() -> None:
    # (1000 + 2000) tokens at 3.75 per token, 500 at 15 per token.
    assert request_bound("synthetic-model", 1000, 500, SYNTHETIC) == 3000 * 3 + 3000 * 3 // 4 + 7500


@pytest.mark.parametrize(("table", "model"), CASES)
def test_the_bound_is_never_below_the_cost_of_a_response_within_it(table: str, model: str) -> None:
    prices = TABLES[table]
    for request_bytes, max_tokens in itertools.product((0, 1, 17, 4096), (1, 3, 1024)):
        bound = request_bound(model, request_bytes, max_tokens, prices)
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
                        inference_geo="global",
                        prices=prices,
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


def _sonnet(geo: str | None, **counts: int) -> int:
    return usage_cost(SONNET, inference_geo=geo, **counts)  # type: ignore[arg-type]


def test_only_global_inference_is_priced_at_1x() -> None:
    counts = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_write_tokens": 1_000_000,
        "cache_read_tokens": 1_000_000,
    }
    base = 2_000_000 + 10_000_000 + 2_500_000 + 100_000
    assert _sonnet("global", **counts) == base
    # US-only is 1.1x on every category; an unknown or unreported geo is charged the same.
    for geo in ("us", "eu", "GLOBAL", "", None):
        assert _sonnet(geo, **counts) == base * 11 // 10, geo
    for category in counts:
        one = {name: 1_000_000 if name == category else 0 for name in counts}
        assert _sonnet("us", **one) == _sonnet("global", **one) * 11 // 10, category


def test_the_premium_rounds_up_once_from_the_exact_sum() -> None:
    # 7 input tokens at 7 micro-USD per MTok: 49 * 1.1 = 53.9 -> 1 micro-USD, never 0.
    assert (
        usage_cost(
            "synthetic-odd", input_tokens=7, output_tokens=0, inference_geo="us", prices=SYNTHETIC
        )
        == 1
    )
    # 1_000_001 output tokens at 11: 11_000_011 * 1.1 / 1e6 = 12.1000121 -> 13.
    assert (
        usage_cost(
            "synthetic-odd",
            input_tokens=0,
            output_tokens=1_000_001,
            inference_geo="us",
            prices=SYNTHETIC,
        )
        == 13
    )
    assert (
        usage_cost(
            "synthetic-odd",
            input_tokens=0,
            output_tokens=1_000_001,
            inference_geo="global",
            prices=SYNTHETIC,
        )
        == 12
    )


@pytest.mark.parametrize(("table", "model"), CASES)
def test_a_non_global_cost_is_never_below_the_global_cost(table: str, model: str) -> None:
    prices = TABLES[table]
    for tokens in itertools.product((0, 1, 999, 1_000_003), repeat=2):
        kwargs = {"input_tokens": tokens[0], "output_tokens": tokens[1], "prices": prices}
        plain = usage_cost(model, inference_geo="global", **kwargs)  # type: ignore[arg-type]
        assert usage_cost(model, inference_geo="us", **kwargs) >= plain  # type: ignore[arg-type]
        assert usage_cost(model, inference_geo=None, **kwargs) >= plain  # type: ignore[arg-type]

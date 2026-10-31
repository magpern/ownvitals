import pytest

from ownvitals.emailnorm import normalize_email


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Owner@Example.COM", "owner@example.com"),
        ("  padded@example.com\n", "padded@example.com"),
        ("already@example.com", "already@example.com"),
    ],
)
def test_ascii_letters_are_lower_cased_and_the_ends_trimmed(given: str, expected: str) -> None:
    assert normalize_email(given) == expected


@pytest.mark.parametrize(
    "address", ["straße@example.com", "İstanbul@example.com", "ÅSA@example.com", "ǅ@example.com"]
)
def test_other_letters_are_kept_as_typed(address: str) -> None:
    # Full Unicode case folding would merge distinct addresses ("ß" and "ss").
    assert normalize_email(address) == address.translate(
        {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}
    )
    assert normalize_email("straße@example.com") != normalize_email("strasse@example.com")

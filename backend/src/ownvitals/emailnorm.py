"""Email normalization (ADR 0003 section 1)."""

_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def normalize_email(value: str) -> str:
    """Trim, then lower-case ASCII letters only; everything else is kept as typed.

    Full Unicode case folding would merge distinct addresses (``ß`` and ``ss``). The result is
    for lookup and uniqueness only; mail goes to the address as typed.
    """
    return value.strip().translate(_ASCII_LOWER)

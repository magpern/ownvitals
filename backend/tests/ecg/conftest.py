"""Fixtures and markers of the ECG tests.

SYNTHETIC: everything under ``tests/ecg`` is generated; no real or public ECG recording exists
in the repository.

The ``slow`` marker (the sweep of at least 200 seeds per condition and rate, ADR 0006's tests
file, "Seeds and other tests") is registered here because ``pyproject.toml`` is a shared file.
Those tests are skipped unless ``OWNVITALS_ECG_SLOW=1``; the conftest of ``tests/`` clears every
other ``OWNVITALS_*`` variable for the tests themselves, but this one is read at collection.
"""

import os

import pytest

SLOW_ENV = "OWNVITALS_ECG_SLOW"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", f"slow: the >= 200-seed ECG sweep, run only with {SLOW_ENV}=1"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(SLOW_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"the >= 200-seed sweep runs only with {SLOW_ENV}=1")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)

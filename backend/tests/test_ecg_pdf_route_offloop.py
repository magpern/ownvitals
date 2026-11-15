"""The ECG PDF route builds the PDF off the event loop (issue #301). Synthetic data only.

``build_pdf`` is replaced by a blocking stand-in; while it runs, a ticker on the same loop must
keep running and the build must be on another thread.
"""

import asyncio
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ownvitals.views import ecg_routes

SYNTHETIC = True


async def test_the_pdf_build_runs_in_a_thread_and_the_loop_keeps_ticking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int] = []

    def slow_build(_facts: object) -> bytes:
        seen.append(threading.get_ident())
        time.sleep(0.3)  # blocks its own thread only
        return b"%PDF-synthetic"

    recording = SimpleNamespace(id=uuid4(), leads=((1, 2, 3),), sampling_hz=250)

    async def own(*_args: object) -> object:
        return recording

    monkeypatch.setattr(ecg_routes, "build_pdf", slow_build)
    monkeypatch.setattr(ecg_routes, "facts_for", lambda *_a: object())
    monkeypatch.setattr(ecg_routes, "_own", own)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        response = await ecg_routes.ecg_pdf(uuid4(), object(), object())  # type: ignore[arg-type]
    finally:
        task.cancel()
    assert response.body == b"%PDF-synthetic"
    assert seen
    assert seen[0] != threading.get_ident()
    assert ticks >= 10  # a blocked loop would have ticked about once

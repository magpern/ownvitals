"""M1 failure injection, the cases no other test file names (PLAN section 4.4; issue #30).

Out-of-order webhooks. The other failure-injection items have their tests in the files that
``test_m1_checklist.py`` maps them to. Synthetic ids only.
"""

import pytest

from jobtools import engine_for
from test_withings_webhook import (  # noqa: F401 - the module's fixture and helpers
    WHEN,
    Cache,
    counts,
    entry,
    factory,
    form,
    link,
    new_app,
    post,
    receiver,
)

pytestmark = pytest.mark.usefixtures("schema")


async def test_out_of_order_webhooks_are_each_durable_and_collapse_into_one_sync(
    factory: object,  # noqa: F811 - the imported fixture
    database_url: str,
) -> None:
    with engine_for(database_url) as engine, engine.begin() as admin:
        app_id = new_app(database_url)
        presented, cached = entry(app_id)
        link(admin, app_id)
    app = receiver(factory, Cache(entries=[cached], read_at=WHEN))  # type: ignore[arg-type]
    newer = form(startdate=1_700_000_300, enddate=1_700_000_400)
    older = form(startdate=1_700_000_000, enddate=1_700_000_100)
    answers = [await post(app, presented, body) for body in (newer, older, newer, older)]
    assert [answer.status_code for answer in answers] == [200, 200, 200, 200]
    with engine_for(database_url) as engine, engine.connect() as admin:
        jobs, events = counts(admin)
    # Two distinct windows are two durable events; the replays add nothing; one singleton job.
    assert (jobs, events) == (1, 2)

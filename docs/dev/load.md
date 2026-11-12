# Rate limiter load test

The load harness of the two rate limiters (#462, PLAN §11 M8 "load test of the rate limiter";
PLAN §4.4, ADR 0005 §8 for Withings, ADR 0003 §9 for auth). It lives in `backend/tests/load/`
and drives many concurrent callers against the real Postgres functions and tables, with a fake
Withings answer and synthetic accounts only. It reports **counts only**: admitted, refused and
the p95 wait. It asserts **admission bounds**, never timings.

Running it on the production host, and judging the numbers for users beyond the household, is
the owner's part of M8. This page is what the owner runs.

## What it checks

| Test | Asserts |
|---|---|
| `test_load_withings_rate.py::test_the_app_budget_holds_under_n_workers_and_m_users` | N worker threads (one app-role connection each) share M accounts on the poll lane until the bucket is empty. Admitted ≤ budget + the refill the run had time for (Postgres clock); admitted + remaining ≤ the same (no lost update); admitted ≥ budget (a drained bucket gave all of it); at most 4 calls in flight; every account served and the shares within 2 of each other (round-robin). Every 7th fake call answers HTTP 429: its retry is a new claim and spends its own token, so the fake saw exactly as many calls as were granted. |
| `…::test_a_601_holds_every_worker_until_the_pause_ends` | Mid-run the fake answers `601` (30 s). The worker pauses the app as `ingest.calls.limited` does, while the fake holds the calls after it (and so their slots) until the pause has committed: at most three more grants get in first. No claim that starts after the pause committed is granted, every worker is given the same resume time, and once the superuser ends the hold (the fake clock) the rest drains within the same bounds. |
| `test_load_auth_ratelimit.py` (7 scenarios) | Concurrent `RateLimiter.begin` calls, one pooled app-role connection per caller: sign-in (and password re-auth, the same buckets), password reset and the TOTP step-up, each from many IPs on one account and from one IP across many accounts, plus the (account, IP) pair. Admitted equals, per fixed window of the binding bucket, the smaller of its reservations and its limit; the limits are pinned as numbers (50, 30, 10, 10, 10, 20, 20). |
| `…::test_the_load_variants_are_wired`, `…::test_the_network_guard_covers_the_load_tests`, `…::test_no_load_test_lifts_the_network_guard` | The CI variant has at least 50 callers and no marker; the full one is marked `load` and deselected by default; the suite's network guard is active and no load test is marked `allow_network`. |

Two profiles drive the same tests:

- **`ci`**: 50 callers, 10 accounts, a Withings budget of 20, one round per auth scenario. It is
  unmarked, so it runs in the normal backend shards on every backend change (a few seconds of
  wall time; each run has a 45 s deadline) and a regression fails `ci`.
- **`full`**: marked `load` and deselected by default (`addopts = "-ra -m 'not load'"` in
  `backend/pyproject.toml`). It runs only with `-m load`.

## Running the full variant

On a throwaway Postgres 17 with the roles and functions installed, as for any database test
([backend.md](backend.md), [isolation.md](isolation.md); the `ci.yml` `backend-tests` steps are
the reference). Never against the live database: the fixtures migrate and drop the schema.

```bash
cd backend
export OWNVITALS_TEST_DATABASE_URL=postgresql+psycopg://postgres:test@127.0.0.1:30002/ownvitals
uvx uv@0.13.6 run pytest -m load tests/load            # the full variant only
uvx uv@0.13.6 run pytest tests/load                    # the CI variant only
```

The sizes are environment variables, read once at collection (the suite removes every other
`OWNVITALS_*` variable before each test):

| Variable | Default | Range | Means |
|---|---|---|---|
| `OWNVITALS_LOAD_WORKERS` | 90 | 50–90 | Concurrent callers, one Postgres connection each |
| `OWNVITALS_LOAD_USERS` | 30 | 2–workers | Synthetic Withings accounts the workers share |
| `OWNVITALS_LOAD_BUDGET` | 100 | 4–100 | The Withings bucket's budget a minute (100 is production's) |
| `OWNVITALS_LOAD_ROUNDS` | 5 | 1–100 | Repeats of each auth scenario, each on fresh buckets |

The worker cap of 90 keeps the run under Postgres's default `max_connections` of 100. With a
budget of 100 a Withings test drains in well under a minute; the deadline of the full profile
is 600 s.

The summary at the end is one line per scenario, for example:

```
withings budget 100, 30 users [full]: callers=90 admitted=109 refused=3447 p95_wait_ms=5128.0
auth sign-in: one account, many IPs [full]: callers=90 admitted=250 refused=650 p95_wait_ms=1740.4
```

Nothing in it is a value, an id or a timestamp. `p95_wait_ms` is for the owner's judgement
only; no test asserts on it.

## Fakes and limits

- Withings is a fake answer in memory (`loadtools.FakeWithings`), read with the real
  `interpret`; nothing leaves the machine, and the network guard of `tests/conftest.py` applies.
- Postgres's clock cannot be faked: the refill bound is computed from the Postgres clock at the
  start and end of the run, and the 601 hold is ended by the superuser moving `paused_until`
  into the past.
- The reset buckets are rebuilt in the test as `reset_routes.request_reset` reserves them; the
  sign-in and step-up buckets come from `password_buckets` and `mfa_buckets`.

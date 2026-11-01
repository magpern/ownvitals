# Withings: the API client and the fake

PLAN §4.1 and §4.2, ADR 0004 §6 to §9, issue #20. The package `ownvitals.withings`
(`backend/src/ownvitals/withings/`) holds the client of the **public** Withings API (D1; never
the app's private API) and nothing else yet: no database, no jobs. Everything below is
IMPLEMENTED against the in-process fake only; no part of it is verified against a real account
(INTEGRATION_VERIFIED is the owner's, after the probe, PLAN-operations §10.4).

| Module | Holds |
|---|---|
| `client.py` | `WithingsApp`, `WithingsClient`, `signature` |
| `errors.py` | the error taxonomy (`TokenInvalid`, `RateLimited`, `Transient`, `Permanent`), `interpret` (an answer into its model or an error), `RetryBudget`, `paginate` |
| `models.py` | one pydantic model per response body |
| `units.py` | `real_value(value, unit)`: `value * 10^unit` as an exact `Decimal` |

## The client

```python
from ownvitals.withings.client import RetryBudget, WithingsApp, WithingsClient

app = WithingsApp(client_id, client_secret)            # base URL per app, never per connection
async with WithingsClient.open(app) as client:          # egress.withings_async_client inside
    body = await client.getmeas(access_token, lastupdate=cursor, budget=RetryBudget(retries=3))
    async for page in client.getmeas_pages(access_token, category=1):
        ...
```

- **Worker only.** `open` builds its HTTP client with `egress.withings_async_client`, which
  refuses to build or send unless `install_worker_allowlist` ran (`OutboundBlocked`): the worker
  and the probe CLI install it first, tests with the reset fixture (below). It reaches only
  `wbsapi.withings.net` and `account.withings.com`, follows no redirect and ends each request
  within 30 s. The web process's one Withings call, the authorization-code exchange (roadmap
  Amendment 1), stays `egress.exchange_withings_code`; #22 reads its answer with
  `interpret("v2/oauth2:requesttoken", status, body, TokenResponse)`, the same taxonomy.
- **Requests.** Every call is a form-encoded `POST`; the access token goes in the
  `Authorization: Bearer` header, never in the URL, and a malformed token (empty, a space, a
  control character) is refused before anything is sent. `WithingsApp.base_url` must be
  `https://` and a Withings host with no path.
- **Answers.** The JSON `status` is checked on every answer (errors come back as HTTP 200).
  JSON is parsed with `Decimal` for non-integers and refuses `NaN`/`Infinity`; the body is
  capped at 32 MiB (`MAX_RESPONSE_BYTES`) and validated by the endpoint's model.
- **Endpoints** (PLAN §4.2): `getmeas`, `heart_list`, `heart_get`, `getactivity`,
  `getintradayactivity`, `getworkouts`, `sleep_get`, `sleep_getsummary`, `getdevice`,
  `getgoals`, `notify_subscribe`, `notify_list`, `notify_update`, `notify_revoke`, `refresh`
  (`oauth2 requesttoken`, refresh grant) and `revoke` (`oauth2 revoke`, signed with a nonce from
  `signature getnonce`: HMAC-SHA256 with the client secret over `action,client_id,nonce`).
- **Paging.** `paginate(fetch)` follows `more`/`offset` and raises
  `Permanent("pagination_stuck")` when the offset does not advance or after 10 000 pages; the
  `*_pages` methods wrap it for `getmeas`, `heart list`, `getactivity`, `getworkouts` and
  `sleep getsummary`. `getintradayactivity` and `sleep get` answer at most 24 h per call: the
  caller walks the windows (#25).

## Errors and retries

| Class | From | Retried by the client |
|---|---|---|
| `TokenInvalid` | API status 401, 2553, 2555; HTTP 401 | never: PLAN §4.3's path (#22) |
| `RateLimited` | API status 601; `wait_seconds` from the body, else 60 s | never: the app's bucket holds (#23) |
| `Transient` | HTTP 408, 429, 5xx; timeouts; transport errors; an answer that is not a JSON object with an integer `status` | within the caller's `RetryBudget` |
| `Permanent` | every other API status, by number (`STATUS_CATEGORIES`: 214 `not_authorized`, 247, 286, 293, 304, 503, 2554; else `unknown_status`); another HTTP status; status 0 with a body that does not fit (`unexpected_body`); `response_too_large`; `pagination_stuck` | never |

- `args == (category,)`, no `__cause__`, no `__context__` (ADR 0004 §8). Categories come from
  numbers and types, never from text; the provider's text never reaches an exception or a log.
  Each error carries `endpoint` (`"measure:getmeas"`), `http_status` and `api_status`.
- Only transport failures are classified: the client catches `egress.TRANSPORT_ERRORS`
  (`TIMEOUT_ERRORS` among them) and nothing else, so a programming error propagates as itself.
- `RetryBudget(retries, max_delay=60, deadline=None)`: exponential backoff from 1 s with ±20 %
  jitter, at least a `Retry-After` hint, no wait past `max_delay` or `deadline`. One budget can be
  shared by the calls of a job; without one nothing is retried.
- **`refresh` is never retried** (PLAN §4.3 step 5) and takes no budget. `error.ambiguous` is true
  for `Transient` and for `unexpected_body`: Withings may have rotated the pair, so the caller
  runs the recovery of step 4 (one retry with the stored refresh token within 8 h).
- `revoke`'s budget covers the nonce only: a nonce is single use.
- Mapping to job categories (ADR 0005 §6, ADR 0004 §8) is the caller's (#22, #25).

## Models

- Unknown fields are kept in `raw` on every model. Token and nonce answers keep only the names of
  unknown fields (a value there could be a secret).
- Enums are plain `int`/`str`, so undocumented values (`attrib` 99, ECG model 93, a new device
  type) are kept as sent; `DOCUMENTED_*` say which values the docs know.
- Optional fields default to `None`, never to 0: missing data is never read as zero (PLAN
  §4.2). Only what identifies a record is required.
- Integer fields are strict (no `bool`, no float); other numbers are `Decimal`. Measurements:
  `Measure.real` and `GoalWeight.real` go through `units.real_value`, the one helper.
- Tokens, nonces and callback URLs (which hold the webhook secret, ADR 0008) are `SecretStr`.
- Accepted spec quirks: `more` an integer or a boolean, `updatetime` and the token's `userid` as
  digit strings, `sleep get`'s `series` as one segment, an empty intraday `series` as `[]`.

## Logging

Logger `ownvitals.withings`: `withings call failed` (WARNING) and `withings call retried` (INFO)
with `endpoint`, `category`, `error_class`, `http_status`, `api_status` (and `delay`). Never a
token, code, nonce, signature, callback URL, user id, measurement or provider text
(`test_withings_client_logs.py`). httpx logs at `WARNING` in the app (`log.QUIET_LOGGERS`).

## Tests and the fake

The client's tests (`tests/test_withings_client.py`, `test_withings_client_errors.py`,
`test_withings_client_logs.py`) run the client over the egress client with the in-process fake of
#19 as its network transport, so every egress check still runs:

```python
@pytest.fixture(autouse=True)
def _worker(monkeypatch):
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)     # ownvitals.egress.core
    egress.install_worker_allowlist(ai_enabled=False)

fake = FakeWithings(seed=3)
app = fake.add_app()
user = fake.add_user(days=20, quirks=True)             # synthetic; quirks: odd enums, partials
tokens = fake.tokens_for(app.client_id, user.userid)
async with WithingsClient.open(
    WithingsApp(app.client_id, app.client_secret), inner=FakeWithingsTransport(fake)
) as client:
    ...
```

The fake (`backend/tests/withingsfake.py` and its modules: `withingscore` protocol,
`withingswire` constants and records, `withingsstore` data endpoints and paging, `withingsnotify`,
`withingsrules` refusals, `withingsdata`/`withingsstreams` the seeded synthetic generator,
`withingsshapes` the documented shapes) answers as both Withings hosts, logs every request with
secrets masked (`fake.log`), and injects failures per request number:
`fake.inject("drop" | "delay" | "status" | "http" | "garbage" | "mutate", at=n, endpoint="measure:getmeas", ...)`.
Everything it serves is synthetic and its answers carry `x-ownvitals-synthetic: true`.

**The fake's readings where the public spec is silent** (PR #63 and #71; each one constant or
place in the fake, each for the probe to confirm), which the client is tested against:

- status codes: 401 unknown or expired access token; 2553 bad or consumed refresh token; 304
  bad, expired or used authorization code; 503 invalid params; 214 missing scope; 293 bad
  callback URL or a failed HEAD; 286 no such subscription; 247 revoke of a user without a grant;
  2554 unknown action or a method other than `POST`; 601 with an optional `body.wait_seconds`
  (none on the fake's own limiter);
- endpoint scopes (`DATA_ROUTES` in `withingsstore.py`); the docs map scopes to applis only;
- token lifecycle: older access tokens live their own 3 h; a retry with the old refresh token
  after a lost answer discards the unseen pair and does not extend the 8 h window; a new
  authorization drops every earlier token; refreshing with the current token ends an older grace
  token; the scope check runs before the first-use mark; the code is single use;
- `lastupdate` is strictly after; paging is index based over the current result (page size 100
  unless set); deletions are never reported;
- `revoke` signs `revoke,client_id,nonce`; `getnonce`'s timestamp is not checked;
- `sleep get` returns at most 24 h; `updatetime` is an integer; `sleep get`'s `series` is a list;
  `more` is an integer for `getmeas` and a boolean elsewhere;
- `notify`: subscribing again updates the comment; several URLs per appli; `update` takes any
  combination of `new_*` and `comment`; callback URLs must be HTTPS on port 443 with a domain
  name and at most 255 characters (else 293);
- requests: `POST` with a form body only, no secret in the query string (else 503); 120 requests
  a minute per app as a token bucket (burst 120);
- not modelled: restricted mode, the Withings+ gate (`blocked_by_provider_plan`), the US medical
  cloud, ECG lead information.

```bash
cd backend
uvx uv@0.13.6 run pytest -q tests/test_withings_client.py tests/test_withings_client_errors.py \
  tests/test_withings_client_logs.py tests/test_withings_fake_*.py
```

## Open (for the probe and later issues)

- The `Permanent` status list is the fake's reading of the public status families; an unlisted
  number is `unknown_status`. The probe (#29) records which statuses a real account returns.
- How a post-13-Oct-2026 account without Withings+ answers (PLAN §4.2,
  `blocked_by_provider_plan`) is unknown; the client has no category for it yet.
- `oauth2 getdemoaccess` (the demo account) is not in the client; the issue that wires the demo
  account adds it.

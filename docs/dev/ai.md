# AI: the cost cap, consent and the tool set

How ownvitals bounds what AI insights cost ([ADR 0014 §7](../adr/0014-ai-insights.md), D11,
PLAN §7.4; #205), how a user consents and withdraws (§3), and the data-minimised tools the model
may call (§4; #206), and the model client with its redacted audit log (§1, §5, §6; #207). Consent
and tools are [below](#consent-and-withdrawal), the client
[after them](#the-model-client-and-the-audit-log), then the chat assistant (#208,
[here](#the-chat-assistant)) and the weekly digest (issue 209, [last](#the-weekly-digest)).

## The pieces

| Path | What |
|---|---|
| `backend/migrations/versions/0013_ai_budget.py` | `ai_reservations` and `ai_spend` (owned, forced RLS, the app role `SELECT` only) and `ai_spend_global` (system, no app privilege) |
| `docker/postgres/ai/50-ai.sql` | the role `ownvitals_ai` and the definer functions `ai_reserve`, `ai_settle`, `ai_release`, `ai_settle_lost`; idempotent, run as the superuser by `db-functions` after `rate.sql` |
| `backend/src/ownvitals/ai/pricing.py` | the pricing table (empty until the owner picks the model) and the per-request bound |
| `backend/src/ownvitals/ai/budget.py` | reserve, settle and release in a job's fenced transaction, the release rule, the reaper call, the month and the paused message |
| `backend/src/ownvitals/worker/loop.py` | `maintain` runs `ai_settle_lost` after the queue reaper while `ai_insights` is on |

## Units, month and caps

- Integer **micro-USD** everywhere. A month is the calendar month in **UTC**, stored as its first
  day; a reservation is charged to its own month whenever it is settled.
- `OWNVITALS_AI_USER_MONTHLY_CAP_MICROUSD` and `OWNVITALS_AI_GLOBAL_MONTHLY_CAP_MICROUSD`: both 0
  by default (no request can be reserved) until the owner sets them (ADR 0014 open question 2).
  Settings refuse a per-user cap above the global one.
- **The settings are the authoritative caps.** The worker passes both on every `ai_reserve`;
  nothing in the database stores a cap, so a change applies to the next reservation. Lowering a
  cap below what is already spent simply refuses every further request that month.

Disconnecting Withings with "delete imported data" (#225) keeps `ai_spend` and
`ai_reservations` (`KEPT` in `ownvitals.connections.purge`): billed spend stays counted, so the
option cannot reset a month's budget. Account deletion removes both through the `owner_id`
cascade; the month's global total keeps the spend.

## The flow of one request

1. Bound it: `pricing.request_bound(model, request_bytes, max_tokens)`. An unpriced model raises
   `ModelUnpriced` (`ai_model_unpriced`): no call.
2. `budget.reserve(fs, bound, settings)` in the job's `fenced_session`, which **commits before the
   call**. `None`: a cap refused it; no call, and the user sees `budget.paused_message(...)`
   ("This month's AI budget for your account is used up. It resets on <date>.").
3. After the call, in a new fenced transaction:
   - completed: `budget.settle(fs, reservation, usage_cost(...))`. `OVERRUN` when the actual is
     above the reservation: charged in full anyway and logged as `ai_cost_overrun` (a count, no
     amount);
   - `budget.may_release(sent=..., status=...)` true (not sent, or 400, 401, 403, 404, 413, 429,
     529): `budget.release(fs, reservation)`;
   - anything else (timeout, broken connection after sending, other 5xx):
     `budget.settle(fs, reservation, reservation.amount)`.
4. A worker that dies or loses its lease between 2 and 3 leaves the reservation open. It keeps
   counting against both caps until `ai_settle_lost` (worker maintenance, every 30 s while
   `ai_insights` is on) sees the attempt is over (the job is gone, not running, under a newer
   `lease_version`, or its lease expired) and charges it at the reserved amount. A later settle
   by that worker finds nothing open and changes nothing.

## The functions

Owned by `ownvitals_ai` (`NOLOGIN BYPASSRLS`, column grants and `DELETE` on `ai_audit` and
`ai_conversations` for the two retention purges, listed in ADR 0005 §3),
`SECURITY DEFINER`, `search_path = pg_catalog, pg_temp`, comment `ownvitals-ai v1`, `EXECUTE` for
`ownvitals_app` only. None takes an owner id: `ai_reserve`, `ai_settle` and `ai_release` act for
the transaction's `app.user_id` and raise `42501` without one. One lock order everywhere: the
month's global row, the user's row, the reservation. Their digests are pinned with the queue's in
`ownvitals/db/queue_digests.py`; `20-queue.sql` admits the six (these four, the audit
retention's `ai_audit_purge`, [below](#audit-retention), and the conversation retention's
`ai_purge_expired`, [below](#conversation-retention)) by name, and the queue check (startup and
every new connection) refuses a database where one differs or one of the six is missing.

### Changing a function

1. Edit `50-ai.sql`, keep it idempotent, bump `ownvitals-ai v<N>` (and `AI_VERSION` in
   `queue_check.py`) if the contract changes.
2. Against a database at head with the db-functions scripts run (`postgres:17.11`):
   `uv run python tests/queue_digests.py --write`.
3. CI's `backend` job installs the scripts twice and runs `--check`.

## Tests

Real PostgreSQL (`OWNVITALS_TEST_DATABASE_URL`, [backend.md](backend.md)), synthetic users and
jobs only, no model call:

| File | Covers |
|---|---|
| `test_ai_cost_cap.py` | 50 concurrent reservations admit exactly 10 (and a sampler sees no moment over a cap); the global cap across users; every function at once without deadlock; double settle, sequential and racing (settle, release or the reaper); release then settle; settle then crash; a crash after reserve; the reaper's rules and limit; January settled in February; overrun; the CHECKs; the app role's privileges; RLS; account deletion |
| `test_ai_functions.py` | each function callable by the app role only (every other role refused); signatures without an owner; guard 2, guard 9, digests and the startup check; the script plain SQL, idempotent, superuser only; drift found, refused and repaired |
| `test_ai_budget.py` | the Python layer through the fence; a lost lease; the release rule; the month and the paused message; the cap settings; the worker's maintenance |
| `test_ai_pricing.py` | no model priced; the bound never below a cost within it |
| `test_ai_migration.py` | migration 0013 alone (no script) leaves the app role read-only |

## Known gaps

- No admin view: an admin-visible aggregate (how many users are at the cap this month) needs a
  read path for `ownvitals_maint` or a count-only definer function; not built.
- An account deleted while a reservation is open leaves its amount in the month's global
  `reserved` (the user rows cascade away); it lowers the global headroom for that month only.
- With `ai_insights` off the reaper does not run, so a reservation left open keeps counting.
- `MonthBudget.paused` reflects the user's cap only: the app role cannot read the global row, so
  a month refused by the global cap shows as not paused. The chat and digest UI issue decides
  what to show then.
- The reaper also settles a reservation whose lease has expired before the queue reaper bumps
  `lease_version`. A worker that is late but alive then finds it settled (`UNCHANGED`), so an
  actual above the bound is not charged; the loss is limited by the request bound and the eval's
  actual-to-bound ratio.
- The overrun is logged (`ai_cost_overrun`, a count), not yet a metric: `metrics.py` comes with
  #167.

## Consent and withdrawal

ADR 0014 §3, #206. AI is **off** for a user unless the effective `ai_insights` flag is on **and**
the user has a live consent to the current text. Every AI entry point checks both
(`ai.consent.require_enabled` / `check_enabled`) and answers "not enabled" otherwise
(`AiNotEnabled`; the tools answer `{"error": "not_enabled"}` and read nothing).

| Path | What |
|---|---|
| `backend/migrations/versions/0021_ai_consent.py` | `ai_consents`, `ai_conversations`, `ai_messages` (owned, forced RLS; at most one live consent per owner; a message belongs to a conversation of the same owner) |
| `backend/src/ownvitals/ai/consent_text.py` | the consent text per language (`en`, `sv`), `CONSENT_VERSION` and the SHA-256 a consent records (pure) |
| `backend/src/ownvitals/ai/consent.py` | give, withdraw, the enabled check, `require_consent` (`FOR SHARE`, for a job's fenced session), `AI_JOB_KINDS` |
| `backend/src/ownvitals/ai/consent_routes.py` | `GET`/`POST`/`DELETE /api/ai/consent` |
| `backend/src/ownvitals/ai/tables.py` | the Core handles and the vocabularies |
| `frontend/src/pages/settings/AiSection.tsx` | the AI row of Settings, "Your data" |

- **The text** is code, one copy per language; the API serves it as it is with its version and
  SHA-256, and a consent names all three. Changing a text means a new `CONSENT_VERSION`: every
  user's consent is then `outdated` and AI is off for them until they consent again. Version 1's
  retention paragraph is a placeholder until the owner records the provider review (ADR 0014
  §2); AI stays off until then (lock 2), and the recorded answer comes as version 2.
- **Why the backend serves a sentence**: responses carry codes, not sentences (ADR 0015 §6),
  with this one exception. The consent records the SHA-256 of the exact text the user saw, so
  the text must come from the one place that hashes it; the frontend shows it as plain text and
  words everything around it from its own catalogue.
- **Give**: `POST /api/ai/consent` with `version`, `locale`, `sha256` and `agree: true`. Needs a
  **step-up** (`require_stepup`). 409 `ai_not_enabled` while the flag is off, 409
  `ai_consent_outdated` for any other text (the flag is checked first, so with the flag off the
  answer is `ai_not_enabled` whatever the text). A live consent to an older text is closed and
  replaced.
- **Withdraw**: `DELETE /api/ai/consent`, a full session only (no step-up), also while the flag
  is off. One transaction: close the live row (its lock first), cancel the user's queued and
  running `ai_chat_turn` / `ai_digest` jobs (`jobs.user.cancel`: a queued job ends at once, a
  running one at its next checkpoint), delete `ai_messages` and `ai_conversations`, and (issue
  209) `ai_digests` and the digest opt-in `ai_digest_prefs`. The consent rows and the spend ledger
  stay (no content).
- **Storing a reply or digest** (the chat and digest issues) first calls
  `consent.require_consent(fs.db, owner)` in the same fenced transaction: the live row is read
  `FOR SHARE`, so a withdrawal waits for the reply to commit and then deletes it, or commits
  first and the reply refuses.
- **Purge**: account deletion removes all three tables (`owner_id` cascade). "Delete imported
  data" (`connections.purge`) deletes the conversation history with the imported rows and keeps
  `ai_consents` (`KEPT`): deleting data is not withdrawing consent.

## The tool set

ADR 0014 §4, #206. `ai.tools.run_tool(factory, ctx, settings, name, arguments, now=...)` runs one
tool for `ctx`'s user, in web or in the worker (a job's `job_run.factory` and `job_run.ctx`).
Arguments are closed sets (`ai.tools_model`); outputs are fixed models on the allowlist
`OUTPUT_FIELDS`. `TOOLSET_VERSION` is bumped with any change to a tool, its arguments or its
output.

| Tool | Arguments | Returns |
|---|---|---|
| `list_metrics` | none | the metrics with daily values: key, unit, area, data state |
| `metric_summary` | `metric`, `period` 30/90/180/365 | days with data, the display band's median and range, the latest value and day, the trend's state, direction, significance and slope per 30 days with its interval, coverage, longest gap |
| `daily_values` | `metric`, `period` 7/14/30/60/90 | `[day, value]`, at most 90, two decimals |
| `findings` | `status` active/history, `period` | rule id, tier, severity, confidence, origin, status, window dates, metric keys; at most 100 |
| `scores` | `kind` readiness/his/body_path | kind, day, status, value, stale |
| `ecg_summary` | `period` 30/90/180/365 | the number of recordings, counts of Withings' classification (`afib`: negative, positive, inconclusive, not_reported, undocumented), and per recording (newest first, at most 90, `truncated`) its day, that classification, Withings' heart rate and the four intervals (ms); ownvitals' estimates (`estimates`: day, HR, RMSSD, SDNN) only past the tier filter |
| `sleep_summary` | `period` 7/14/30/60/90 | per night (the wake-up's local day), at most 90: total, deep, light and REM sleep (s), wake-ups, efficiency, sleep score, average heart rate |
| `activity_summary` | `period` 7/14/30/60/90 | per day, at most 90: steps, distance and elevation (m), soft, moderate and intense activity (s), active kcal |

- **Reads** only through `views.reads` (the read API's reader and assembly; for sleep and
  activity `Reader.stream_days`, #401), `views.findings_read` (consumer `ai_context`: hidden
  findings absent; `read_ecg_estimates`) and `views.ecg_read.list_recordings` (Withings' own ECG
  fields; the list read never loads the signal), each a `user_session` of the caller under forced
  RLS. The ECG, sleep and activity handlers are `ai.tools_vitals`, their models
  `ai.tools_vitals_model`. No tool writes, takes a user argument or returns an id, email,
  Withings or device identifier, subject, evidence, free text or time of day.
- **Tier filter**: experimental findings and the HIS only when the owner-only switch is on **and**
  `OWNVITALS_AI_INCLUDE_EXPERIMENTAL` is true (default false); otherwise the reads run with the
  switch off, so they are never loaded. ownvitals' ECG estimates (ADR 0006 §9) follow the same
  filter: `ecg_summary` returns `EcgSummaryExperimentalOut` (the one entry of `OUTPUT_VARIANTS`)
  only when an up-to-date estimate passes it, otherwise `EcgSummaryOut`, which has no
  `estimates` field at all (absent, not empty).
- **Sleep and activity** read the normalised stream rows (`sleep_*`, `activity_*`,
  `source_type IS NULL`) with the scores pass's rules: `unconfirmed`, `implausible` and
  `duplicate` rows left out, the local day (the profile's fixed zone, else the row's), the later
  row of a day winning. The fields are a closed subset; the units (seconds, metres, kcal) are
  Withings' public documentation's, a documented assumption until the probe shows them. The
  period ends today (UTC).
- `TOOLSET_VERSION` is `2` since #401: an `ai_eval_ref` that names tool set 1 no longer opens
  lock 3, so the owner's eval run must be repeated.
- A result over 64 KiB is `{"error": "result_too_large"}`; an unknown tool `unknown_tool`; bad
  arguments `invalid_arguments`.

| Test file | Covers |
|---|---|
| `test_ai_consent.py` | the text and its fingerprint; give needs the flag, a step-up, the exact text and the ticked box; outdated consent; the flag and consent checks; withdrawal deletes history, cancels queued and running AI jobs only, keeps the ledger and the other user's rows, needs no step-up and works with the flag off; the reply/withdrawal race on the consent row |
| `test_ai_tools_schema.py` | the pinned output schema (any extra field fails; the ECG variant too), `check_fields`, closed arguments, refusals, no planted identifier, zone, signal id or text in any output over every tool and argument, the ECG/sleep/activity values and days (a duplicate row left out), the 64 KiB cap as an error, and an eval reference of tool set 1 refused |
| `test_ai_tools_tiers.py` | experimental items and ECG estimates for each combination of the switch and the setting, a stored "off", hidden findings, a stale estimate, `not_enabled` without the flag, without consent and with an outdated consent |
| `test_ai_tools_isolation.py` | two users, in the request context and in a worker job run by the runner: no row of the other user |
| `frontend/src/pages/settings/AiSection.test.tsx` | the read-only state, plain-text consent text, the unticked box, the request body, outdated (consent again or withdraw), withdrawal, and withdrawal of a given or outdated consent while the flag is off |

### Known gaps (consent and tools)

- `sleep_summary` and `activity_summary` return the normalised per-day values; the app shows no
  sleep or activity values yet (its tiles show the stream state only), so "the aggregates the
  user sees in the app" (ADR 0014 §4) are these until a sleep or activity screen exists (#401,
  delegated and provisional).
- `ecg_summary` pages through `views.ecg_read.list_recordings` (200 a page) until the period's
  start; no counts query was added.
- `scores` returns no components (they are free-form JSON); the device attribution of device
  findings is not returned.
- ADR 0003 §4's step-up table does not list "Give AI consent" yet (ADR 0014 Consequences).

## The model client and the audit log

ADR 0014 §1, §5, §6, §7, #207. `ai.client.call_model(job, settings, kind=..., messages=...)` is the
only code that sends a model request; `ai.turn.run_turn` is the tool loop around it. Both run in
the worker, inside a job run (`JobRun`), never in web.

| Path | What |
|---|---|
| `backend/src/ownvitals/ai/requests.py` | the fixed system prompt (`PROMPT_VERSION`), `MAX_TOKENS` (chat 1,024, digest 2,048), the closed message blocks, `build_request`, `serialize`, `check_body`, `eval_ref` (pure) |
| `backend/src/ownvitals/ai/responses.py` | `parse_reply`: text and tool requests only, the usage (pure) |
| `backend/src/ownvitals/ai/gates.py` | `check_locks` (the owner's locks in the settings), `read_api_key`, `ModelRefused`, `ModelFailed` |
| `backend/src/ownvitals/ai/client.py` | `call_model`: the order of checks, the attempts within one reservation, settle or release, the audit row |
| `backend/src/ownvitals/ai/turn.py` | `run_turn`: tool requests run through `ai.tools.run_tool` as the job's user, at most 8 per turn |
| `backend/src/ownvitals/ai/audit.py` | the `ai_audit` row and log line, the `ai-audit` HKDF key, the fingerprints |
| `backend/migrations/versions/0023_ai_audit.py` | `ai_audit` (owned, forced RLS, the app role `SELECT` and `INSERT` only; token counts in `*_token_count` columns) |
| `backend/tests/fakemodel.py` | the fake Messages API every AI test uses (the `inner` transport of the egress client) |

### Settings and secrets

All empty by default, so **no call is made** until the owner sets every one (ADR 0014 open
questions 1, 3 and 6). Agents never set them.

- `OWNVITALS_AI_MODEL`: the pinned model id (lower case, digits, `.` and `-`). It must also be in
  `ai.pricing.PRICES`, which is empty until the owner picks the model (`ai_model_unpriced`).
- `OWNVITALS_AI_PROVIDER_REVIEW_REF`: the reference of the recorded provider review (lock 2).
- `OWNVITALS_AI_EVAL_REF`: exactly `<model>/<PROMPT_VERSION>/<TOOLSET_VERSION>` as the owner's
  evaluation run passed it (lock 3). Changing the model, the prompt or the tool set makes it stale,
  and AI is off again until a new run passes.
- The secret file `anthropic_api_key` (never the environment, never a setting): read by the client
  at call time from the secrets directory; without it `ai_not_configured`. `app_secret_key` must
  be present too (the audit key); without it `ai_not_configured`.

### The order of one call

1. `egress.model_async_client()` is built first: outside a worker installed with AI on it raises,
   and the call is `ModelRefused("outbound_blocked")` before any database or network socket.
2. `check_locks`: `ai_insights` (environment), `ai_model`, the review and evaluation references,
   the price; then the API key file and the audit key. No I/O but the key file.
3. `build_request` and `check_body`: the bytes hold no UUID, email address, API key or app secret
   (`identifier_in_request`, `email_in_request`, `secret_in_request`); then the bound.
4. One fenced transaction: the job's cancel, `consent.require_enabled` (the stored flag and the
   live consent `FOR SHARE`) and `budget.reserve`. It commits before the call. Refused:
   `cancelled`, `ai_not_enabled` or `ai_budget_exhausted`, and nothing was sent or kept reserved.
5. Up to three attempts under that one reservation, each in `job.provider_call` (the lease
   outlasts the 300 s total deadline). Only a connection failure before sending, a 429 or a 529
   is retried (after `retry-after` up to 30 s, else 2 s then 8 s), and a later attempt re-checks
   cancel, flag and consent first. Anything else ends the call.
6. Settle at the usage's cost (`settled` or `overrun`), release (`released`: not sent, 400, 401,
   403, 404, 413, 429, 529, a withdrawal or cancel between attempts) or charge the reserved amount
   (`charged`: a timeout, a broken connection after sending, any other status, a malformed
   answer). The settlement and the `ai_audit` row commit together in one fenced transaction
   (the session guard's reviewed path for job writes), with one `ai call` log line. A lease lost
   before or at that point (during a re-check too) leaves the reservation to the reaper and
   writes the log line only (`settlement: lost`): the attempt is no longer this worker's.

`ModelRefused(code)`: nothing was sent and nothing stays reserved. `ModelFailed(code)`: a request
was made and failed; `code` is the audit's error category (`rate_limited`, `overloaded`,
`refused`, `server_error`, `http_other`, `timeout`, `transport`, `not_sent`, `bad_response`,
`consent`, `cancelled`). The chat and digest jobs map them to their job outcome.

### The request

Exactly `model`, `max_tokens`, `system`, `tools` and `messages`: no `metadata`, no user id. The
messages are the conversation's last 20 text messages (`history_window`, starting with the user)
and, in a turn, the tool exchanges. A tool call is echoed back with our id (`call_<n>`) and its
**validated** arguments only (invalid ones become `{}`); a tool result must validate as that
tool's output model or `ToolError` (`tool_result_off_schema`). Text is 1 to 16,000 characters.
The API key is only in the `x-api-key` header.

### The answer and the tool loop

`parse_reply` keeps `text` and `tool_use` blocks and the usage (four non-negative integers);
anything malformed is `bad_response` and charged. `run_turn` runs each requested tool for the
job's user through `run_tool` (which re-checks the flag and consent itself) and sends the results
back. An unknown tool name ends the turn with `unknown_tool`, more than 8 tool calls with
`tool_limit`; neither stores an answer. Nothing the model says authorises anything else.

### The audit row

`ai_audit`: job, kind (`chat`, `digest`; the table also admits `eval` for ADR 0014 §9), model,
prompt and tool-set versions, the requested tools' names with their validated arguments, token
counts, cost in micro-USD, reservation, latency, attempts, status, error category, HTTP status,
settlement, and HMAC-SHA-256 of the request bytes and of the answer bytes under
HKDF(`app_secret_key`, `ai-audit`). Never message text, tool results, the system prompt or the key.
The log line carries the same fields without the cost and the fingerprints. The app role may only
read and insert (append-only; `10-roles.sql` and `50-ai.sql` restore it). It survives withdrawal
and "delete imported data" (`KEPT`) and goes with the account.

### Audit retention

ADR 0014 §6, #331. Rows are kept `OWNVITALS_AI_AUDIT_RETENTION_MONTHS` (default 12, from 12 to
120: longer, never shorter) and then deleted by the worker's hourly purge tick
(`worker.upkeep.purge`, every `purge_interval_seconds`, after the auth purge and whether or not
`ai_insights` is on). `ai.audit_retention.purge_expired_audit` calls `ai_audit_purge(p_months,
p_limit)` (`50-ai.sql`, `SECURITY DEFINER`, owned by `ownvitals_ai`, `EXECUTE` for the app role
only) in a session with no user, at most 1,000 rows per transaction and 10 transactions per tick,
oldest first; it returns a count, logged as the `ai audit purge` maintenance step. The function
refuses fewer than 12 months (`22023`), so the app role cannot shorten the retention, and it reads
only `ai_audit.id` and `created_at`. Without `50-ai.sql` nothing is called (no audit row can exist
without `ai_reserve`).

| Test file | Covers |
|---|---|
| `test_ai_audit_purge.py` | rows past retention go for every owner, younger ones stay, the 12-month boundary, a longer setting, the limit oldest first, a user context changes nothing; a shorter retention or a limit out of bounds refused (function and Python); the app role still cannot delete; batches; not installed; the setting; the worker tick with AI on and off, and one failing step not stopping the other |
| `test_ai_functions.py` | with the family: callable by the app role only, pins, digests, drift (a purge without the floor, the function dropped, the role's `ai_audit` grants) |

### Conversation retention

ADR 0014 §5, #395. A conversation last updated (its last message) more than
`OWNVITALS_AI_CONVERSATION_RETENTION_DAYS` days ago (default 90, from 30 to 90: never kept
longer than the ADR's 90 days) is deleted with its messages by the same hourly purge tick, after
the audit purge and whether or not `ai_insights` is on (the `ai conversation purge` step).
`ai.conversation_retention.purge_expired_conversations` calls `ai_purge_expired(p_days, p_limit)`
(`50-ai.sql`, owned by `ownvitals_ai`, `EXECUTE` for the app role only) in a session with no
user, at most 500 conversations per transaction and 10 transactions per tick, oldest first,
served by migration 0027's index on `ai_conversations (updated_at, id)`; it returns a count. The
role reads only `ai_conversations.id` and `updated_at` and may `DELETE` there; the messages go by
the foreign key's cascade (which runs as the table owner, outside row security). The function
refuses fewer than 30 or more than 90 days (`22023`): a caller with the app role can neither keep
history longer nor wipe every user's recent history. The age is checked again on the row the
`DELETE` locks, so a reply that holds the conversation's lock and touches `updated_at` keeps it.
Without `50-ai.sql` nothing is called; withdrawal and account deletion still remove history.

| Test file | Covers |
|---|---|
| `test_ai_conversation_purge.py` | expired conversations go with their messages for every owner, the boundary to the minute, the limit oldest first; **two users**: one user's expired history goes and the other's young history stays whole, a caller's context changes nothing, the app role alone reaches only its own rows; a conversation touched while the purge waits on its lock stays; a retention or limit out of bounds refused (function and Python); batches; not installed; the setting; the worker tick with AI on and off, with the setting, and a failing step not stopping it |
| `test_ai_functions.py` | the sixth function with the family: pins, digest, drift (a purge without the floor, dropped, the role's `ai_conversations` grants) |

| Test file | Covers |
|---|---|
| `test_ai_client.py` | from web, AI off in the worker, no or outdated consent, the flag off (environment and stored), over either cap, each owner lock, unpriced, no key file, a cancelled job, a body failing the scan: zero requests, no reservation, no audit row; one call settled at its cost; retries within one reservation (429, 529, connect); the retry limit; 400 to 413 released; 500, 502, timeout and a malformed answer charged; withdrawal or cancel between attempts; overrun; a lost lease; the audit append-only |
| `test_ai_client_requests.py` | the body's fields and prompt, the history window, malformed messages, closed tool arguments and our ids, results off schema, the tool-call limit, the scan, answer parsing |
| `test_ai_egress_scan.py` | every tool and argument set on a planted account through `run_turn`: no secret, id, email, Withings text, stored message or sample in any body the fake receives; the key only in its header; unknown tool and tool limit end the turn |
| `test_ai_client_redaction.py` | a planted question, tool result and answer reach no log line and no `ai_audit` column; the fingerprints are keyed |

### Known gaps (client)

- `compose.yaml` mounts `anthropic_api_key` and `app_secret_key` into the worker, the key there
  only (`test_compose_ai_key.py`, [stack.md](stack.md#secrets)); `scripts/worktree.sh init`
  leaves the key file empty, so the local stack is `ai_not_configured`. With the defaults AI is
  off anyway.
- No SDK: the guard refuses the provider's SDK in `src`, so the client posts the Messages API's
  JSON itself through `model_async_client` (`anthropic-version: 2023-06-01`). The request and
  answer shapes are from the public API documentation, not verified against the provider.
- The `eval` kind's writer (ADR 0014 §9) is not built.
- `ai_audit.created_at` defaults to `now()` but the app role inserts the row and could set it: a
  user could date their own rows into the future to keep them past retention (their own rows
  only, like T103a). The purge scans `created_at` without an index of its own (the index is
  `(owner_id, created_at)`); fine at household scale, a migration can add one if it is not.
- The app role may insert its own `ai_audit` rows (forgery of one's own audit only; threat model
  T103a).
- Guard registrations: `ownvitals.auth.keys.derive_key` is on `auth_tables_scan.PUBLIC_ENTRY_POINTS`
  (the `ai-audit` key; no table, no other name). The system prompt avoids the guarded table words.

## The chat assistant

ADR 0014 sections 3, 5, 8 and 9, PLAN section 7.4, #208. Web stores the user's message and queues
an `ai_chat_turn` job in one transaction; the worker answers; the browser follows the job's event
stream (`/api/jobs/{id}/events`: states and a closed category, never text, T47) and then reads the
answer. Web never calls the model.

| Path | What |
|---|---|
| `backend/src/ownvitals/ai/chat_model.py` | `CHAT_KIND`, `ChatInput` (ids only), `failure_for` (the job category of a turn without an answer), the request id, the history rows as messages (pure) |
| `backend/src/ownvitals/ai/chat_store.py` | web side, the caller's `user_session`: create, list, read and delete conversations, `post_message`, the budget state |
| `backend/src/ownvitals/ai/assistant_routes.py` | `/api/ai/conversations[/{id}[/messages]]` and `/api/ai/budget` |
| `backend/src/ownvitals/ai/assistant.py` | the worker's `ai_chat_turn`: read through the fence, `turn.run_turn`, store in the final fenced transaction |
| `backend/migrations/versions/0025_ai_job_failures.py` | three job failure categories: `ai_not_enabled`, `ai_budget_exhausted`, `ai_unavailable` (none retried) |
| `frontend/src/components/assistant/*`, `frontend/src/pages/AssistantPage.tsx`, `frontend/src/api/assistant.ts` | the `/assistant` page (design: `docs/design/README.md`, "Assistant") |

### The API

| Route | What |
|---|---|
| `GET /api/ai/conversations` | the user's conversations, most recently used first (also while AI is off) |
| `POST /api/ai/conversations` | a new one; 409 `ai_not_enabled` without the flag or a live consent |
| `GET /api/ai/conversations/{id}` | its newest 200 messages in order and its latest turn (`job_id`, `state`, `failure`) |
| `POST /api/ai/conversations/{id}/messages` | `content` (1 to 16,000 characters, not blank) and `request_id` (16 to 59 of `A-Za-z0-9_-`); 202 with `message_id` and `job_id` |
| `DELETE /api/ai/conversations/{id}` | cancels its running turn and deletes it with its messages (no step-up) |
| `GET /api/ai/budget` | `paused` and `resets_on` (no amounts) |

Another user's conversation, an unknown one and a malformed id are the same 404. A message is
refused, writing nothing, in this order: `ai_not_enabled` (the flag and a live consent, the consent
row `FOR SHARE`), a reused `request_id` with another text (`idempotency_mismatch`; the same text
answers the first message and job), `not_found`, `ai_turn_in_progress` (one turn at a time per
conversation), `ai_budget_exhausted` (the user's month is used up), `busy` (the per-user job
limit), then the chat limit `rate_limited` (`AI_CHAT_ACCOUNT`, 20 accepted messages per hour per
user, ADR 0014 section 8; a refusal takes no slot).

**Creating a conversation** has its own limit, `rate_limited` (`AI_CONVERSATION_ACCOUNT`, 30
accepted creations per hour per user; `ratelimit.py`, reserved through `reserve_ai_conversation`
after the consent check, so a refused creation takes no slot). ADR 0014 section 8 sets only the
message limit; the owner delegated this value to the Architect (#352), who kept 30 per hour: it
is above any real use (one conversation per topic) and low enough that a script cannot fill the
history table. Changing it is a one-line edit of the `Limit`.

### One turn

1. Read the conversation's newest 40 messages in a fenced transaction; the job's message must be
   the newest and the user's, else `invalid_input` (a deleted conversation, a crafted job of
   another user: RLS returns nothing) and no call.
2. `run_turn` (the client checks every lock, the flag, consent and the budget before each call).
   A refusal or failure maps to `ai_not_enabled`, `ai_budget_exhausted`, `outbound_blocked` or
   `ai_unavailable`; a cancel (withdrawal, delete) stops the run (`RunStopped`). A turn that ends
   with `unknown_tool`, `tool_limit` or no text is `ai_unavailable`. Nothing is stored then.
3. `Complete(store)`: with a cancel requested meanwhile the runner cancels instead. The store reads
   the consent `FOR SHARE NOWAIT`, locks the conversation, inserts the answer, touches
   `updated_at`.

**Lock order.** A withdrawal locks the consent row, then the job rows, then deletes the history.
A fenced transaction holds its job row from its first statement, so every consent read inside one
(the client's reservation and re-check, the store) uses `consent.require_consent(...,
nowait=True)`: a consent row a withdrawal holds answers "not enabled" at once and the transaction
rolls back, instead of closing a lock cycle. A store that meets a withdrawal this way (the row is held, or already closed) raises `RunStopped("cancelled")`, and the runner rolls the store back and ends the job `cancelled`, not `failed` with `ai_not_enabled` (#352); a flag off or an outdated consent still fails the job. A conversation delete takes the job rows before the
conversation, as the store does.

### Tests

Real PostgreSQL, synthetic users, the fake model behind the real egress client (`tests/aichat.py`):

| File | Covers |
|---|---|
| `test_ai_assistant.py` | a message answered by the runner and read through the API; the job holds ids only; the history window is the conversation's own; a tool turn stores only the answer; provider failure, unknown tool, tool limit, retries within one reservation, the cap reached after queueing, consent withdrawn after queueing: nothing stored; **withdrawal mid-turn** (the stream ends `cancelled`, a reconnect gets 204, the history is gone); the store's `NOWAIT` against a withdrawal holding the row; a job of one user naming another's conversation (worker RLS) |
| `test_ai_assistant_routes.py` | **RLS**: another user's conversation cannot be read, continued or deleted, the list holds only one's own; every refusal and its code; the chat limit; a refusal takes no slot; delete cancels the running turn; a turn of a deleted conversation |
| `ai_evals/test_ai_evals.py` | the CI eval suite (ADR 0014 section 9), each category at 100 %: **privacy** (no cross-user data or identifier over every tool, no experimental finding or HIS without the switch, the fixed system prompt, the history window, no read of another user's conversation), **prompt injection** (injections in the user's text, planted in device text fields, unknown tools, out-of-range arguments, active content in answers, the tool limit), **consent and withdrawal** (none, outdated, withdrawn after queueing, before the call, mid-call, between tool steps, the stored flag off, consenting again starts empty); a failing case fails its category |
| `frontend/src/components/assistant/*.test.tsx` | answers render as text (links, images, scripts, headings stay characters); the consent prompt, a turn followed to its end, the cap, a failed turn, the chat limit, delete, a withdrawal while a turn runs closes the stream and hides the history; the Settings link |

### Known gaps (assistant)

- The request scan (`requests.check_body`) refuses a UUID or an email address anywhere in the
  body, the user's own text included: such a message fails the turn as `ai_unavailable` ("couldn't
  answer") with nothing sent. The page does not say why.
- A month refused by the global cap only shows at the turn (`ai_budget_exhausted` as its failure):
  `GET /api/ai/budget` reflects the user's cap (the app role cannot read the global row).
- A transient database error during a turn retries the job (`upstream_unavailable`, up to
  `max_attempts`), which can make a second model call; each call is reserved and charged on its
  own, within the caps.
- The model evals against the real model (`python -m ownvitals.ai.eval`, ADR 0014 section 9) are
  not built: the categories that need the model's judgement (health claims, grounding,
  insufficient data) wait for that module.
- The assistant has no rail item (a design change for the owner); it is reached from Settings.

## The weekly digest

ADR 0014 sections 3, 5 and 7, ADR 0010 section 8 (the nerve monthly summary), PLAN section 7.4,
issue 209. Once a week the worker writes a digest for a user who opted in; the browser reads it
on the assistant page, and one outbox mail says that it is waiting. Web never calls the model and
never queues a digest.

| Path | What |
|---|---|
| `backend/src/ownvitals/ai/digest_model.py` | `DIGEST_KIND`, `DigestInput` (the week only), the digest week, the weeks a job may still write, the fixed request, `ending_for` (pure) |
| `backend/src/ownvitals/ai/digest.py` | the worker: `queue_if_due` and `then_digest` (when), `run_digest` (one run), the store |
| `backend/src/ownvitals/ai/digest_store.py` | web side, the caller's `user_session`: the view and the opt-in |
| `backend/src/ownvitals/ai/digest_routes.py` | `GET`/`PUT /api/ai/digest` |
| `backend/migrations/versions/0026_ai_digest.py` | `ai_digest_prefs` and `ai_digests` (owned, forced RLS), the outbox kind `ai_digest_ready` and route `assistant` |
| `frontend/src/components/assistant/DigestView.tsx`, `frontend/src/api/digest.ts` | the digest card under the conversations of `/assistant` |

### When a digest is queued

There is no cross-user scheduler query. The worker's registry wraps the findings notification pass
(`notify_findings`, one owner, run after every rule evaluation of the 6-hourly poll and the nightly
reconciliation) with `then_digest`: after each successful pass, `queue_if_due` asks for that
owner only and queues one `ai_digest` job (origin `scheduler`, singleton `ai_digest`,
`max_attempts` 1, input `{"week_start": "YYYY-MM-DD"}`) when **all** hold:

1. `gates.check_locks`: `ai_insights` on in the environment, the model, the review and evaluation
   references and the price. **With the defaults nothing is ever queued.**
2. In the pass's fenced session: the stored flag and a live consent to the current text
   (`consent.require_enabled(..., nowait=True)`: the fence holds the job row first).
3. The owner's opt-in row exists, and no digest is stored for the week.

The week is the last full ISO week (Monday to Sunday, UTC) before the day of the pass. A job may
still write that week or the three before it (`week_ok`); a later or future week is
`invalid_input`.

### One run

1. The week already stored, or no opt-in any more: the job completes without a call.
2. `turn.run_turn(kind="digest")` with the one fixed user message of `digest_model.request` (the
   period, the tools to read, the nerve metrics' monthly summary from `metric_summary` period 30
   and the findings tool, quiet copy without nudges; dates only, no user data). The system
   prompt is the chat's; 2,048 output tokens. The client checks every lock, the flag, the consent
   and the budget before each call.
3. The ending (`ending_for`): an answer that fits (1 to 16,000 characters) is stored `ready` and
   queues one `ai_digest_ready` outbox row (route `assistant`, `/assistant`; the row has no
   column for the text, the mail only says a summary is waiting). The cap stores the week as
   `skipped_budget` (no call, no mail; the page says the week was skipped). A call that may have
   been sent and failed, an unknown tool, the tool limit, no text or an answer too long store
   `unavailable` (no mail), so a failing provider is tried once a week, never every six hours. A
   missing consent or flag (`ai_not_enabled`), an owner lock (`ai_unavailable`) or the egress
   block (`outbound_blocked`) fail the job and store nothing; a cancel stops it.
4. The store, in the job's final fenced transaction: the consent `FOR SHARE NOWAIT` (a withdrawal
   that holds or closed the row: the job ends `cancelled`, nothing stored), the opt-in `FOR SHARE` (gone:
   nothing stored), the insert (`ON CONFLICT (owner_id, week_start) DO NOTHING`), then the mail.

Lock order as the chat's: the job row, the consent row without waiting, the opt-in and the digest.
A withdrawal takes the consent row, the job rows, then deletes the digests and the opt-in, so
consenting again starts with the digest off.

### The API

| Route | What |
|---|---|
| `GET /api/ai/digest` | `available` (the flag and a live consent), `enabled` (the opt-in), the newest 12 digests: `week_start`, `status` (`ready`, `skipped_budget`, `unavailable`), `content` (only when ready) and `created_at`. Also while AI is off |
| `PUT /api/ai/digest` | `{"enabled": bool}` (strict, closed). Opting in: 409 `ai_not_enabled` unless AI is enabled for the user (the consent row `FOR SHARE` in the same transaction); no step-up (the consent was given with one). Opting out always works and keeps the stored digests |

### Data lifecycle

- **Withdrawal** deletes `ai_digests` and `ai_digest_prefs` with the conversation history.
- **Delete imported data** (`connections.purge`) deletes `ai_digests` (`IMPORTED`: text about that
  data) and keeps `ai_digest_prefs` (`KEPT`: a setting, like the consent).
- **Account deletion**: both through the `owner_id` cascade.
- **Export** (`reports.export_allowlist`): `ai_digests` (id, week, status, text, created) and
  `ai_digest_prefs` (id, opted in at).

### Tests

Real PostgreSQL, synthetic users, the fake model behind the real egress client (`tests/aidigest.py`
on top of `tests/aichat.py`):

| File | Covers |
|---|---|
| `test_ai_digest_model.py` | the week, the late weeks, the fixed request (the nerve metrics, quiet copy, the body scan), every ending, the bound, the input |
| `test_ai_digest.py` | a digest written, stored and announced without its text; a stored week or an opt-out makes no call; an invalid week; no consent, the stored flag off, an owner lock, over the cap (skipped): no call; a failed turn stored `unavailable` once; withdrawal mid-call (cancelled, the digests and opt-in deleted) and of a queued job; the store's `NOWAIT` against a withdrawal holding the row, after a committed one, after an opt-out; one user's job never reads or writes another's digest (worker RLS) |
| `test_ai_digest_queue.py` | no second digest while one runs; the notification pass queues one job with the week only (singleton, one attempt) and none once stored; each owner lock, the stored flag, no, outdated or withdrawn consent, no opt-in (another's does not count), a stored week: nothing queued; the defaults never queue; the worker wraps the pass |
| `test_ai_digest_routes.py` | the view is the user's own (RLS); opt-in needs AI enabled and a current consent, opt-out always; the closed body; a session needed; withdrawal, "delete imported data" and account deletion; the mail holds no health content in any language |
| `ai_evals/test_digest_evals.py` | the digest's eval categories, each at 100 %: **privacy** (no cross-user data or identifier over every tool, the tier filter, the fixed request without history, another user's digest untouched), **prompt injection** (planted device text, unknown tools, out-of-range arguments, active content stored as text, the tool flood, an answer cannot mail its text), **consent** (none, outdated, the stored flag off, withdrawn during the call and between tool steps, consenting again starts with the digest off) |
| `frontend/src/components/assistant/DigestView.test.tsx` | off by default and the request body, a ready digest as text with the label, skipped and failed weeks, the toggle disabled while AI is not available, a refused opt-in, nothing read without consent |

Mutation checks run for the PR (each fails a test above): the store waiting on the consent row,
the store without the consent check or the opt-in check, the queue without the consent or the
locks or the opt-in, withdrawal keeping the digests or the opt-in, the cap stored as unavailable,
the opt-in without the enabled check, the purge keeping the digests, retries, the pass not
wrapped.

### Assumptions and known gaps (digest)

- **The design**: no mock-up covers the digest. It is built only from existing components (the
  Settings row and toggle in a card, `MessageText`, the AI label), under the conversations of
  `/assistant`, with no unread badge and no new rail item; the wording is ours. The Architect
  and the owner may change both (AGENTS.md "UI design").
- **The mail goes whatever `notify_email` says**, like `export_ready`: opting in to the digest
  is the request; the mail says only that a summary is waiting. The toggle's help text says so
  (#355 finding 5; the owner may still prefer to honour `notify_email`, a one-line change).
- **A second digest job is not queued while one runs** (`digest._queueable`): the singleton only
  collapses queued jobs, so a run in progress is checked as well, and a week is not billed twice.
- **"Delete imported data" cancels the user's AI jobs first** (`consent.cancel_ai_jobs`, job rows
  before the history), so a digest or reply being stored commits and is then deleted, and a queued
  one never runs.
- **A skipped week shows the reset date** only while the month's budget is still used up (the
  budget read of the page); an older skipped week keeps the sentence without a date.
- **The digest is written in English** (the instruction says so): the worker does not read the
  account language. A Swedish digest needs the locale passed into the request and a second
  instruction, and a new `PROMPT_VERSION` if the system prompt changes.
- **`PROMPT_VERSION` is not bumped**: the system prompt is unchanged. The digest instruction is a
  new fixed text the owner's eval run (lock 3) must cover before an `ai_eval_ref` is set; with
  none set, AI is off anyway.
- **Users without a Withings connection get no digest**: the pass that queues it runs per
  connection. They have no data to summarise.
- **One try per week**: a failure after sending (or a refused body in a later tool step) stores
  `unavailable`; the next week's digest is the retry. A transient error before sending is stored
  the same way.
- **Opting out keeps the stored digests**; withdrawal deletes them.
- The digest's model judgement (grounding, health claims, insufficient data) waits for the
  owner's `python -m ownvitals.ai.eval`, as the chat's does.

## Not built yet

Each waits for its own scheduled issue (none is filed until it is scheduled):

- `ecg_summary`, `sleep_summary` and `activity_summary` (ADR 0014 section 4): they need read views
  the API does not have yet.
- The real-model evals (`python -m ownvitals.ai.eval`, ADR 0014 section 9) and the `eval` audit
  kind's writer: the categories that need the model's judgement.
- A mock-up for the digest card and a rail item for the assistant (owner design decisions).

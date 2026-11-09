# AI: the cost cap, consent and the tool set

How ownvitals bounds what AI insights cost ([ADR 0014 §7](../adr/0014-ai-insights.md), D11,
PLAN §7.4; #205), how a user consents and withdraws (§3), and the data-minimised tools the model
may call (§4; #206), and the model client with its redacted audit log (§1, §5, §6; #207). The
chat and digest jobs come with their own M7 issues and extend this file. Consent and tools are
[below](#consent-and-withdrawal), the client [after them](#the-model-client-and-the-audit-log).

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

Owned by `ownvitals_ai` (`NOLOGIN BYPASSRLS`, column grants only, listed in ADR 0005 §3),
`SECURITY DEFINER`, `search_path = pg_catalog, pg_temp`, comment `ownvitals-ai v1`, `EXECUTE` for
`ownvitals_app` only. None takes an owner id: `ai_reserve`, `ai_settle` and `ai_release` act for
the transaction's `app.user_id` and raise `42501` without one. One lock order everywhere: the
month's global row, the user's row, the reservation. Their digests are pinned with the queue's in
`ownvitals/db/queue_digests.py`; `20-queue.sql` admits the four by name, and the queue check
(startup and every new connection) refuses a database where one differs or one of the four is
missing.

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

- `ai_purge_expired()` (ADR 0014 §5's 90-day conversation retention) belongs with the
  conversation table and is not in this family yet.
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
  running one at its next checkpoint), delete `ai_messages` and `ai_conversations`. The consent
  rows and the spend ledger stay (no content).
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

- **Reads** only through `views.reads` (the read API's reader and assembly) and
  `views.findings_read` (consumer `ai_context`: hidden findings absent), each a `user_session` of
  the caller under forced RLS. No tool writes, takes a user argument or returns an id, email,
  Withings or device identifier, subject, evidence, free text or time of day.
- **Tier filter**: experimental findings and the HIS only when the owner-only switch is on **and**
  `OWNVITALS_AI_INCLUDE_EXPERIMENTAL` is true (default false); otherwise the reads run with the
  switch off, so they are never loaded. No tool returns an ECG estimate.
- A result over 64 KiB is `{"error": "result_too_large"}`; an unknown tool `unknown_tool`; bad
  arguments `invalid_arguments`.

| Test file | Covers |
|---|---|
| `test_ai_consent.py` | the text and its fingerprint; give needs the flag, a step-up, the exact text and the ticked box; outdated consent; the flag and consent checks; withdrawal deletes history, cancels queued and running AI jobs only, keeps the ledger and the other user's rows, needs no step-up and works with the flag off; the reply/withdrawal race on the consent row |
| `test_ai_tools_schema.py` | the pinned output schema (any extra field fails), `check_fields`, closed arguments, refusals, and no planted identifier or text in any output over every tool and argument |
| `test_ai_tools_tiers.py` | experimental items for each combination of the switch and the setting, a stored "off", hidden findings, `not_enabled` without the flag, without consent and with an outdated consent |
| `test_ai_tools_isolation.py` | two users, in the request context and in a worker job run by the runner: no row of the other user |
| `frontend/src/pages/settings/AiSection.test.tsx` | the read-only state, plain-text consent text, the unticked box, the request body, outdated (consent again or withdraw), withdrawal, and withdrawal of a given or outdated consent while the flag is off |

### Known gaps (consent and tools)

- `ecg_summary`, `sleep_summary` and `activity_summary` (ADR 0014 §4) are not built: the read API
  has no sleep or activity daily values yet, and an ECG summary needs a counts read in
  `views.ecg_read`. They come with the read views they need.
- `scores` returns no components (they are free-form JSON); the device attribution of device
  findings is not returned.
- `ai_purge_expired()` (90-day conversation retention) and `ai_digests` are not built.
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
read and insert (append-only; `10-roles.sql` restores it). It survives withdrawal and "delete
imported data" (`KEPT`) and goes with the account.

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
- The 12-month audit retention purge and the `eval` kind's writer (ADR 0014 §9) are not built.
- The app role may insert its own `ai_audit` rows (forgery of one's own audit only; threat model
  T103a).
- Guard registrations: `ownvitals.auth.keys.derive_key` is on `auth_tables_scan.PUBLIC_ENTRY_POINTS`
  (the `ai-audit` key; no table, no other name). The system prompt avoids the guarded table words.

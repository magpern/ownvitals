# AI: the per-user monthly cost cap

How ownvitals bounds what AI insights cost: [ADR 0014 §7](../adr/0014-ai-insights.md) (D11,
PLAN §7.4). Only the cap exists so far (#205); the model client, consent, the tools, the jobs and
the audit log come with their own M7 issues and extend this file.

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
| `test_ai_cost_cap.py` | 50 concurrent reservations admit exactly 10 (and a sampler sees no moment over a cap); the global cap across users; every function at once without deadlock; double settle; release then settle; settle then crash; a crash after reserve; the reaper's rules and limit; January settled in February; overrun; the CHECKs; the app role's privileges; RLS; account deletion |
| `test_ai_functions.py` | each function callable by the app role only (every other role refused); signatures without an owner; guard 2, guard 9, digests and the startup check; the script plain SQL, idempotent, superuser only; drift found, refused and repaired |
| `test_ai_budget.py` | the Python layer through the fence; a lost lease; the release rule; the month and the paused message; the cap settings; the worker's maintenance |
| `test_ai_pricing.py` | no model priced; the bound never below a cost within it |

## Known gaps

- `ai_purge_expired()` (ADR 0014 §5's 90-day conversation retention) belongs with the
  conversation table and is not in this family yet.
- No admin view: an admin-visible aggregate (how many users are at the cap this month) needs a
  read path for `ownvitals_maint` or a count-only definer function; not built.
- An account deleted while a reservation is open leaves its amount in the month's global
  `reserved` (the user rows cascade away); it lowers the global headroom for that month only.
- With `ai_insights` off the reaper does not run, so a reservation left open keeps counting.

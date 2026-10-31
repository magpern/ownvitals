# ADR 0008: Webhook-secret lifecycle, rotations and subscriptions

Status: Accepted (2026-10-31, #72). The text of §1 was accepted in ADR 0004 §4 (#4) and amended
there by #49; #72 moved it here verbatim, so that ADR 0004 stays within the 500-line limit.
§2's single-flight rule moved here from ADR 0004 §4 in #72's first review round and gained
the isolation level (amended 2026-10-31, #72). From now on both change only through an
amendment of this ADR.

## Context

ADR 0004 §4 decides the webhook secrets of PLAN §4.4: derived from a seed in the KEK ring,
stored only as peppered HMACs, verified by web, and written only through one reviewed
`SECURITY DEFINER` function. The #41 review's follow-ups (#49) added the entry lifecycle, two
rotation orders and the clean-up of subscriptions left on old callback URLs; the #58 review's
follow-ups (#72) add input rules and single flight to the function. With all of them in it,
ADR 0004 would pass the repository's 500-line limit for a file (CLAUDE.md).

The lifecycle text is the part of ADR 0004 §4 that stands on its own: it says when an entry
verifies and when it goes, in which order a rotation acts, and what happens to subscriptions on
old URLs. It uses the keys, the verification and the function without defining them. So it
moves here, and the derivation, the two keys, verification, the function and its input rules
stay in ADR 0004 §4.

## Decision

### 1. Lifecycle, rotations and subscriptions (moved from ADR 0004 §4)

The list below is byte-identical to lines 251 to 308 of ADR 0004 at commit `b90ab9a`, the
end of its §4 as #49 left it. Read it with these conventions:

- a bare section reference (§3, §7, §9) points into ADR 0004; "routine step 1", "the deadline
  below" and "the lifecycle above" point into this list;
- "the function" and "the same function" are ADR 0004 §4's webhook-secret function. Its input
  rules and the rotation lock (§2) hold for every call below, and its compare-and-clear for
  retire-all and the append that clears a request, as ADR 0004 §4 says (amended 2026-10-31,
  #72);
- the "amended 2026-10-31, #49" markers refer to ADR 0004's Amendments.

- **Entry lifecycle** (amended 2026-10-31, #49). An entry verifies from `active_from` until it
  is retired (`retire_at`), and then never again. A retired entry is kept only while the worker
  still needs its URL to re-point: `notify update` names a subscription by its current callback
  URL, which the worker rebuilds from the entry and its seed. Once no subscription uses it (each
  one re-pointed, revoked, or past the deadline below), the function deletes it. A pepper
  retires, and its file goes, when no remaining entry references its `pepper_id`; a seed file
  goes when no remaining entry names its `seed_id`. Both are checkable with one query.
- **Routine rotation** (PLAN §4.4; order amended 2026-10-31, #49): append, wait for
  `active_from`, re-point, retire, delete.
  1. the worker appends an entry for a new `secret_id` under the current seed and pepper, with
     `active_from` at least the entry-cache TTL in the future, so every web process holds the
     entry before any subscription points at it;
  2. only after `active_from`, it re-points every subscription with `notify update` (per user,
     with that user's token). A connection's re-pointing retries with backoff for up to
     **14 days**; a connection still failing then is marked to resubscribe on its next
     successful token use or reconnect (below), and scheduled sync covers it meanwhile;
  3. once each subscription on the old entry is re-pointed, revoked or past that deadline, it
     retires the old entry and then deletes it (lifecycle above).

  A seed rotation is such a rotation. **The old seed** stays in the worker, used only to
  rebuild the old URL for re-pointing, until every subscription on the old secret is
  re-pointed, revoked or past the deadline; then it is removed. A routine request that arrives
  while a rotation is in flight waits for it to finish.
- **Compromise rotation** (`rotation_reason = compromise`: after a seed compromise, or a
  compromise of the KEK ring that holds the seed; Architect decisions, 2026-10-31; order
  amended by #49): retire, append, re-point, delete. A new seed and pepper are installed as
  current first (as in §3 step 2).
  1. **retire verification first**: one atomic retire-all call retires every non-retired entry
     (each counts as compromised), so no partial retire exists; a failed call is retried, with
     the alert. It also cancels the queued `notify update` jobs of a routine rotation in flight,
     which this one replaces (a cross-user cancel, so a queue definer function, ADR 0005 §3);
     each such job re-checks its target entry when it runs and stops if it is retired;
  2. it appends an entry under the new seed and pepper, as in routine step 1;
  3. it re-points every subscription whose connection still has a token, as in routine step 2,
     but takes each subscription's current URL from `notify list`, not from its own records.
     After a KEK-ring compromise the wipe left no token, so nothing is re-pointed and reconnect
     revokes the old subscriptions (§3; lingering subscriptions below);
  4. it deletes each old entry once no subscription uses it (at the deadline at the latest);
     then the old pepper and seed are dropped. They are already known, so keeping them until
     then adds no exposure.

  **A refused step** (for example "at most two not retired") raises an alert and is not retried
  silently. Retiring comes first and is atomic, so a refusal never leaves a compromised secret
  verifying: webhooks are ignored until the new entry is active, and scheduled sync covers it.
- **Pepper rotation** needs no new secret and no `notify update`: install the new pepper as
  current; the worker re-hashes every entry under it through the same function (it derives each
  secret from its seed, which every remaining entry still has) and sets its `pepper_id`, one
  entry at a time. The old pepper then retires as the lifecycle above says.
- **Lingering subscriptions** (amended 2026-10-31, #49). A subscription left on an old URL is
  answered with the generic 200, so Withings never cancels it. `notify list` finds it: its
  callback is under our callback base but isn't the current one. #22 revokes such
  subscriptions on connect and reconnect, before it subscribes; #26 (reconciliation)
  resubscribes a connection marked at the 14-day deadline, with the same clean-up, and finds
  subscriptions left on old URLs; #27 (disconnect) revokes every subscription under our
  callback base that `notify list` returns, which covers one past the deadline (§7).
- **`notify list` responses** carry plaintext callback URLs, so webhook secrets. They are never
  stored (not in `raw`, job rows or events) and never logged or used as a metric label; a
  log-redaction test covers them (#22).

### 2. Single flight (moved from ADR 0004 §4)

Moved from ADR 0004 §4 in #72's first review round, to make room there. "The function" is
ADR 0004 §4's webhook-secret function, and "(above)" points to its compare-and-clear.

- **Single flight** (amended 2026-10-31, #72). Each step a rotation takes in the upkeep (ADR
  0005 §8) is one short transaction that first takes `pg_advisory_xact_lock(4, <app id>)`
  (namespace 4, webhook-secret rotation, ADR 0005 §9), then reads the request and the entries,
  then makes its one call to the function **inside that same transaction**, so the lock is held
  from the read to the commit. The function takes the same lock itself before its row lock, so
  no call runs outside it; within the worker's transaction that second take stacks on the
  first (advisory transaction locks are re-entrant within a session) and both are released at
  the commit. A step makes no Withings call. Each step decides from
  what it read under the lock, never from an earlier read, so several workers act on one app
  one step at a time, each on what the step before left; a `rotate` that lands between a
  step's read and its call is refused as `stale_request` (above).

  **Each step runs at READ COMMITTED** (amended 2026-10-31, #72). Under `REPEATABLE READ` a
  step's snapshot is taken at its first statement, the lock call, so its read would predate
  the lock grant and could miss the step before. That fails safe (the function's row lock
  raises a serialization error), but a step must not run that way: the worker sets READ
  COMMITTED on each step's transaction, and the function checks `transaction_isolation` and
  refuses any other level.

## Alternatives

- **Keep the text in ADR 0004:** with #72's amendments the file would pass 500 lines.
- **Move all of ADR 0004 §4:** the derivation and the two keys are part of the KEK ring that
  ADR 0004 §3 describes, and the function's input rules belong next to its privileges and its
  callers; moving them would split one mechanism across two ADRs.
- **Reword the text while moving it:** a reviewer could no longer diff the move. Changes go in
  as marked amendments instead.

The alternatives to the decisions themselves (webhook secrets stored encrypted, or generated
and never kept) stay in ADR 0004's Alternatives.

## Consequences

- ADR 0004 §4 keeps a pointer here. The references to the rotation orders, the lifecycle,
  lingering subscriptions and `notify list` responses in ADRs 0002, 0004 and 0005 and in the
  threat model point here (#72).
- The issue split is ADR 0004's (Consequences): #24 the rotations and the entry lifecycle; #22
  the `notify list` clean-up on connect and reconnect; #26 the resubscribe after the deadline;
  #27 the revoke at disconnect.
- §2 (amended 2026-10-31, #72): #24 runs each step as it says, sets READ COMMITTED and owns
  the stuck-rotation alert (threat model T38, accepted risk 21); #21 builds the function's own
  take of the lock and its isolation check. ADR 0005 §9's row for namespace 4 points here.
- Threat model T32, T38, T41, T43 and accepted risks 20 and 21 cite this ADR.

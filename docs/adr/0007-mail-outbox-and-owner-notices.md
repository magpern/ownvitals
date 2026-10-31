# ADR 0007: Mail outbox and owner notices

Status: Accepted (2026-10-31, #72). It records the Architect's decisions on the non-blocking
findings 1 and 2 of the #58 review (its Opus PASS at `f15a46d`).

## Context

ADR 0003 §6, as #49 amended it, keeps SMTP out of the worker. Mail goes out through one mail
module, from web and from the operator CLI. Mail that originates in the worker (the finding
notifications of threat model T59 and O8, and owner notices such as PLAN §4.3's) goes into a
mail outbox that web sends. The #58 review found two gaps in that text:

- **No recipient rule and no read path** (finding 1). Nothing said what a row may hold. If a
  row could carry an address or free text, any code holding the app role, the worker included,
  could have web mail anything to anyone. That would undo "SMTP never from the worker" for
  exfiltration (T48) and T59's "no values". And web's background sender holds no
  `UserContext`, so under forced RLS it can't read owned rows (ADR 0002 §4: no ambient
  authority).
- **M1 relied on a later outbox** (finding 2). ADR 0004 §6 sent PLAN §4.3's owner notice "in-app,
  plus mail through the outbox" in M1, while ADR 0003 §6 has the notifications issue (M6/M7)
  build the outbox.

## Decision

### 1. What a row holds

Apart from its id, its timestamps and the send state that the functions of §3 keep, an outbox
row holds exactly:

- a **`kind`** from a fixed list that the code defines (an enum the database checks); each kind
  has one template (§2);
- its **recipient**: the `owner_id` of an owned row, under forced RLS (ADR 0002 §2), for mail
  to a user; or no one, in a system row (ADR 0002 §3), for an **owner notice**, which goes to
  the owner, that is the admins (ADR 0004 §6);
- **at most a link**, chosen from a fixed list of the app's routes: a key naming the route,
  never a URL.

There is no address, subject, free text, measurement value or finding text. ADR 0002's guard 1
admits no table whose `owner_id` is optional, so this is two tables under the same rule, one
owned and one system; the notifications issue names them.

An owned row is written in its owner's session (inside a job, a fenced write, ADR 0005 §4), so
the policy's `WITH CHECK` stops a job from queuing mail for another user.

### 2. How web sends it

A background task in the web process sends the rows. For each one it:

1. resolves the recipient's address through `ownvitals.auth` (guard 8) at the time it sends:
   the account's verified email (ADR 0003 §1), or the admins' addresses for an owner notice.
   No address is ever stored in the outbox;
2. builds the mail from the kind's **fixed template**, which says that something is waiting
   and links to the app (T59), with the link built from `OWNVITALS_PUBLIC_BASE_URL` (T12);
3. sends it through the guard-listed mail module to the configured SMTP server (ADR 0003 §6).

The mail module (#6) and the SMTP host are unchanged, so web's outbound connections stay those
of [roadmap Amendment 1](../roadmap.md) (T48).

### 3. Reading the rows

The sender holds no `UserContext` and gets none. It claims and marks rows only through a
reviewed **`SECURITY DEFINER` function pair**: one claims a batch, the other marks a row sent
or failed. As for the other definer families (ADR 0002 §3, ADR 0005 §3):

- the pair is owned by its own `NOLOGIN` role, with `BYPASSRLS` (it reads every user's owned
  rows) and only the column privileges the pair needs on the two outbox tables: no account
  table, no health table;
- it takes no owner id from the caller and returns the minimum: for each claimed row its id,
  `kind`, route key and, for an owned row, the `owner_id` the address is resolved from. The mark
  takes a row id and an outcome;
- it is installed by `db-functions` and pinned, and `ownvitals_app` gets `EXECUTE`. The
  notifications issue adds the family to ADR 0005 §3 with its role, grants and digests.

### 4. Owner notices, and when mail starts

- **In M1, PLAN §4.3's "notify the owner" is in-app only** (ADR 0004 §6): #22 stores the
  notice, and it is shown on the admin page and to an admin at sign-in. It is not mailed: the
  worker never opens SMTP, and the outbox doesn't exist yet.
- **Mail through the outbox starts when the notifications issue (M6/M7) builds it**: the two
  tables, the function pair and its role, the sender and the templates. From then on, mail that
  originates in the worker goes through it, owner notices included.
- The operator CLI's notices (the KEK or seed wipe, ADR 0004 §3) are not worker mail: the CLI
  sends them itself, through the mail module (ADR 0003 §6).

## Alternatives

- **A row carrying the address, subject or rendered text** (the reading #49's text left open):
  any app-role code could have web mail anything to any address (T48, T59).
- **SMTP from the worker:** a mail host on the worker's egress list, next to Withings and
  Anthropic, would be a way out for anything the worker reads (ADR 0003 §6).
- **The sender reading rows with a `UserContext` per owner:** it would need a context issued
  in web from row data, a third issuer, which ADR 0002 §1 rules out.
- **The sender as `ownvitals_maint`:** that login belongs to the admin area, is read-only by
  default and audited on every use (ADR 0002 §4); a background task would widen it to every
  user's outbox rows.
- **The outbox in M1, for the owner notice:** two tables, a definer family and templates for one
  notice that the admin page already shows; the notifications issue builds them once, for every
  kind.

## Open questions

For the notifications issue (M6/M7), within §1 to §3: the tables' names and columns, the list
of kinds and their templates (with the threat model's O8), retries and a row that is claimed
but never marked, how long sent rows are kept, and how fast the sender sends.

## Consequences

- ADR 0003 §6 and ADR 0004 §6 point here and say the same: in-app only in M1, mail through
  the outbox from M6/M7.
- #22 (M1) stores and shows the in-app owner notice and builds no outbox.
- The notifications issue (M6/M7) builds both tables, the function pair with its role (an
  amendment to ADR 0005 §3), the sender and the templates, with tests that a row can't hold an
  address or free text, that a job can't queue mail for another user, and that web's outbound
  list doesn't change.
- Any code that can call the pair holds the app role, which can already set `app.user_id` and
  read every owned row (threat model T24), so the pair shows it nothing new. A caller other
  than the sender could claim rows and so suppress their mail: denial of service only.
- Threat model T48 and T59 cite this ADR.

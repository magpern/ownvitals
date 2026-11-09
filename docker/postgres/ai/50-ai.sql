-- The AI cost cap's role and SECURITY DEFINER functions (ADR 0014 section 7, ADR 0005 section 3,
-- ADR 0002 sections 3 and 5; #205), and the audit log's retention purge (ADR 0014 section 6;
-- #331).
--
-- Run as the superuser, after `alembic upgrade head` and after 20-queue.sql, in ONE transaction.
-- Plain SQL: no psql meta-commands and no transaction control. Migrations never create these
-- functions (handing a function to another role needs the superuser).
--
-- Idempotent: running it again repairs the NOLOGIN role ownvitals_ai (attributes, memberships,
-- settings), revokes and re-grants exactly its privileges, replaces the five functions with their
-- owner, ACL and version comment, and revokes the app role's write privileges on the AI tables
-- again (ai_audit: SELECT and INSERT only).
--
-- Units: integer micro-USD. Month: the calendar month in UTC, stored as its first day.
-- The family (ADR 0014 section 7):
--   ai_reserve(p_amount, p_job_id, p_user_cap, p_global_cap) -> reservation id, or NULL when the
--       month's spent + reserved + amount would exceed either cap (no call is made then);
--   ai_settle(p_id, p_actual) -> true when an open reservation was settled at p_actual (charged in
--       full, even above the reservation: it was billed);
--   ai_release(p_id) -> true when an open reservation was released (the request provably was not
--       processed);
--   ai_settle_lost(p_limit) -> count: the reaper settles, at their full amount, open
--       reservations whose job attempt is over (job gone, not running, a newer lease, or the lease
--       expired). A cross-user path;
--   ai_audit_purge(p_months, p_limit) -> count: deletes, at most p_limit, the ai_audit rows
--       older than p_months months (the worker's ai_audit_retention_months; never below the
--       12 months of ADR 0014 section 6, so no caller can shorten the retention). The other
--       cross-user path; it reads and deletes nothing but ai_audit rows past retention.
-- None takes an owner id: the owner is the transaction's app.user_id, the UserContext user that
-- row-level security reads. No user context: refused. A reservation can be settled or released
-- once; a second call, or one after the reaper, finds no open row and changes nothing.
--
-- The caps are the worker's settings (ai_user_monthly_cap_microusd and
-- ai_global_monthly_cap_microusd), passed on every reservation: the settings are the
-- authoritative source and a cap change applies to the next reservation. No cap is stored.
--
-- One lock order in every function: the month's global row, then the user's row of that month,
-- then the reservation row. A function that starts from a reservation id reads its month without
-- a lock first, then locks in that order, so the four budget functions cannot deadlock one another
-- (ai_audit_purge locks no budget row).
--
-- The role has BYPASSRLS (the reaper reads every user's reservations and jobs) and only the
-- column privileges the four budget functions need on the three AI tables and on jobs, and the
-- id and age columns of ai_audit with DELETE on it for the purge: no account table, no health
-- table. 20-queue.sql admits these five functions by name; their bodies are pinned by
-- digest (ownvitals/db/queue_digests.py) and checked by web and the worker at startup.

DO $$
BEGIN
  IF NOT coalesce((SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user), false)
  THEN
    RAISE EXCEPTION 'this script must run as the superuser';
  END IF;
  IF pg_catalog.to_regclass('public.ai_reservations') IS NULL
     OR pg_catalog.to_regclass('public.ai_spend') IS NULL
     OR pg_catalog.to_regclass('public.ai_spend_global') IS NULL
     OR pg_catalog.to_regclass('public.ai_audit') IS NULL
     OR pg_catalog.to_regclass('public.jobs') IS NULL THEN
    RAISE EXCEPTION 'migrate to head first: an AI table or jobs is missing';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_app') THEN
    RAISE EXCEPTION 'ownvitals_app does not exist; run docker/postgres/init/10-roles.sql first';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_ai') THEN
    CREATE ROLE ownvitals_ai NOLOGIN;
  END IF;
END $$;

-- No login and no other power than BYPASSRLS for its own functions; a member of no role and
-- nobody a member of it; no role settings.
ALTER ROLE ownvitals_ai WITH NOLOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE
  NOREPLICATION NOINHERIT CONNECTION LIMIT -1 PASSWORD NULL;
ALTER ROLE ownvitals_ai RESET ALL;

DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT d.datname FROM pg_catalog.pg_database d
             JOIN pg_catalog.pg_db_role_setting s ON s.setdatabase = d.oid
            WHERE s.setrole = 'ownvitals_ai'::regrole LOOP
    EXECUTE format('ALTER ROLE ownvitals_ai IN DATABASE %I RESET ALL', r.datname);
  END LOOP;
  FOR r IN SELECT g.rolname AS granted, m.rolname AS member, gr.rolname AS grantor
             FROM pg_catalog.pg_auth_members am
             JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
             JOIN pg_catalog.pg_roles m ON m.oid = am.member
             JOIN pg_catalog.pg_roles gr ON gr.oid = am.grantor
            WHERE 'ownvitals_ai' IN (g.rolname, m.rolname) LOOP
    EXECUTE format('REVOKE %I FROM %I GRANTED BY %I CASCADE', r.granted, r.member, r.grantor);
  END LOOP;
END $$;

-- ownvitals_ai owns the AI budget functions and nothing else, and has no default privileges of
-- its own. Anything else is not repaired automatically (dropping or reassigning it could lose
-- data): the script stops and names it.
DO $$
DECLARE
  q oid := 'ownvitals_ai'::regrole;
  r record;
BEGIN
  FOR r IN SELECT s.classid::regclass::text AS catalog, s.objid, s.dbid,
                  pg_catalog.pg_describe_object(s.classid, s.objid, 0) AS what
             FROM pg_catalog.pg_shdepend s
            WHERE s.refclassid = 'pg_catalog.pg_authid'::regclass AND s.refobjid = q
              AND s.deptype = 'o' LOOP
    IF r.dbid <> (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database())
       OR r.catalog <> 'pg_proc'
       OR NOT EXISTS (SELECT FROM pg_catalog.pg_proc p
                        JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
                       WHERE p.oid = r.objid AND n.nspname = 'public'
                         AND p.proname IN ('ai_reserve', 'ai_settle', 'ai_release',
                                           'ai_settle_lost', 'ai_audit_purge')) THEN
      RAISE EXCEPTION 'ownvitals_ai owns % (catalog %, object %); it may own only the '
        'AI budget functions: drop or reassign it by hand',
        coalesce(r.what, '?'), r.catalog, r.objid;
    END IF;
  END LOOP;
  IF EXISTS (SELECT FROM pg_catalog.pg_default_acl WHERE defaclrole = q) THEN
    RAISE EXCEPTION 'default privileges exist FOR ROLE ownvitals_ai; remove them by hand';
  END IF;
END $$;

-- Revoke every privilege ownvitals_ai holds, found through pg_shdepend (every ACL entry naming
-- a role is recorded there, for every object kind). A kind this block does not know stops the
-- script: repair it by hand rather than run with an unknown privilege.
DO $$
DECLARE
  q oid := 'ownvitals_ai'::regrole;
  d record;
  g record;
  target text;
BEGIN
  FOR d IN SELECT s.classid::regclass::text AS catalog, s.objid, s.objsubid, s.dbid
             FROM pg_catalog.pg_shdepend s
            WHERE s.refclassid = 'pg_catalog.pg_authid'::regclass AND s.refobjid = q
              AND s.deptype = 'a'
              AND s.dbid IN (0, (SELECT oid FROM pg_catalog.pg_database
                                  WHERE datname = current_database())) LOOP
    IF d.catalog = 'pg_class' AND d.objsubid = 0 THEN
      SELECT CASE WHEN c.relkind = 'S' THEN 'SEQUENCE ' ELSE 'TABLE ' END || c.oid::regclass::text
        INTO target FROM pg_catalog.pg_class c WHERE c.oid = d.objid;
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor
                 FROM pg_catalog.pg_class c, aclexplode(c.relacl) a
                WHERE c.oid = d.objid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON %s FROM ownvitals_ai CASCADE', target);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_class' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, t.attname
                 FROM pg_catalog.pg_attribute t, aclexplode(t.attacl) a
                WHERE t.attrelid = d.objid AND t.attnum = d.objsubid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL (%I) ON TABLE %s FROM ownvitals_ai CASCADE',
                       g.attname, d.objid::regclass::text);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_namespace' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, n.nspname
                 FROM pg_catalog.pg_namespace n, aclexplode(n.nspacl) a
                WHERE n.oid = d.objid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM ownvitals_ai CASCADE', g.nspname);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_proc' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor
                 FROM pg_catalog.pg_proc p, aclexplode(p.proacl) a
                WHERE p.oid = d.objid AND a.grantee = q AND p.proowner <> q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON ROUTINE %s FROM ownvitals_ai CASCADE',
                       d.objid::regprocedure::text);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_database' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, b.datname
                 FROM pg_catalog.pg_database b, aclexplode(b.datacl) a
                WHERE b.oid = d.objid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON DATABASE %I FROM ownvitals_ai CASCADE', g.datname);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_default_acl' THEN
      FOR g IN SELECT x.defaclrole::regrole::text AS owner, x.defaclnamespace,
                      CASE x.defaclobjtype WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES'
                           WHEN 'f' THEN 'FUNCTIONS' WHEN 'T' THEN 'TYPES'
                           WHEN 'n' THEN 'SCHEMAS' END AS kind
                 FROM pg_catalog.pg_default_acl x WHERE x.oid = d.objid LOOP
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %s %s REVOKE ALL ON %s '
                       'FROM ownvitals_ai CASCADE', g.owner,
                       CASE WHEN g.defaclnamespace = 0 THEN ''
                            ELSE 'IN SCHEMA ' || quote_ident(g.defaclnamespace::regnamespace::text)
                       END, g.kind);
      END LOOP;
    ELSE
      RAISE EXCEPTION 'ownvitals_ai holds a privilege on % %; revoke it by hand',
        d.catalog, d.objid;
    END IF;
  END LOOP;
END $$;

-- Exactly these privileges (ADR 0014 section 7): USAGE on public; on the three AI tables the
-- columns the four budget functions read, insert and update; on jobs the columns that tell whether
-- the attempt that reserved is still running; on ai_audit its id and created_at, and DELETE, for
-- the retention purge (the only DELETE). Everything else was revoked above.
GRANT USAGE ON SCHEMA public TO ownvitals_ai;

GRANT SELECT (id, owner_id, state, lease_version, lease_expires_at)
  ON TABLE public.jobs TO ownvitals_ai;

GRANT SELECT (month, reserved, spent) ON TABLE public.ai_spend_global TO ownvitals_ai;
GRANT INSERT (month) ON TABLE public.ai_spend_global TO ownvitals_ai;
GRANT UPDATE (reserved, spent, updated_at) ON TABLE public.ai_spend_global TO ownvitals_ai;

GRANT SELECT (owner_id, month, reserved, spent) ON TABLE public.ai_spend TO ownvitals_ai;
GRANT INSERT (owner_id, month) ON TABLE public.ai_spend TO ownvitals_ai;
GRANT UPDATE (reserved, spent, updated_at) ON TABLE public.ai_spend TO ownvitals_ai;

GRANT SELECT (id, owner_id, month, amount, state, job_id, lease_version)
  ON TABLE public.ai_reservations TO ownvitals_ai;
GRANT INSERT (owner_id, month, amount, job_id, lease_version)
  ON TABLE public.ai_reservations TO ownvitals_ai;
GRANT UPDATE (state, actual, ended_at) ON TABLE public.ai_reservations TO ownvitals_ai;

GRANT SELECT (id, created_at) ON TABLE public.ai_audit TO ownvitals_ai;
GRANT DELETE ON TABLE public.ai_audit TO ownvitals_ai;

-- The app role reads its own reservations and spend (forced RLS) and writes neither; it has no
-- privilege on the global row (ADR 0014 section 7). The migration grants the same, and so does
-- 10-roles.sql's restore list.
REVOKE ALL ON TABLE public.ai_spend_global FROM ownvitals_app, PUBLIC;
REVOKE ALL ON TABLE public.ai_spend FROM ownvitals_app, PUBLIC;
REVOKE ALL ON TABLE public.ai_reservations FROM ownvitals_app, PUBLIC;
GRANT SELECT ON TABLE public.ai_spend TO ownvitals_app;
GRANT SELECT ON TABLE public.ai_reservations TO ownvitals_app;
-- The audit log stays append-only for the app role (migration 0023, ADR 0014 section 6): only
-- ai_audit_purge deletes, past retention.
REVOKE ALL ON TABLE public.ai_audit FROM ownvitals_app, PUBLIC;
GRANT SELECT, INSERT ON TABLE public.ai_audit TO ownvitals_app;

-- Any other function under one of the five names (an overload) goes: each must exist once.
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT p.oid::regprocedure::text AS signature
             FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public'
              AND p.proname IN ('ai_reserve', 'ai_settle', 'ai_release', 'ai_settle_lost',
                                'ai_audit_purge')
              AND p.oid::regprocedure::text NOT IN (
                'ai_reserve(bigint,uuid,bigint,bigint)', 'ai_settle(uuid,bigint)',
                'ai_release(uuid)', 'ai_settle_lost(integer)',
                'ai_audit_purge(integer,integer)') LOOP
    EXECUTE format('DROP ROUTINE public.%s', r.signature);
  END LOOP;
END $$;

-- Reserve p_amount for one request of the caller's running job. The job must be the context
-- user's, running, with an unexpired lease; its fencing token is recorded, so the reaper can tell
-- when this attempt is over. Locks the month's global row, then the user's row, and only if
-- spent + reserved + p_amount <= cap on both adds p_amount to both reserved counters and inserts
-- the reservation. NULL: refused by a cap.
CREATE OR REPLACE FUNCTION public.ai_reserve(
    p_amount bigint, p_job_id uuid, p_user_cap bigint, p_global_cap bigint)
  RETURNS uuid
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  v_owner uuid := NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid;
  v_now timestamp with time zone := pg_catalog.clock_timestamp();
  v_month date := pg_catalog.date_trunc('month', v_now AT TIME ZONE 'UTC')::date;
  v_lease bigint;
  v_g_reserved bigint;
  v_g_spent bigint;
  v_u_reserved bigint;
  v_u_spent bigint;
  v_id uuid;
BEGIN
  IF v_owner IS NULL THEN
    RAISE EXCEPTION 'no user context' USING ERRCODE = '42501';
  END IF;
  IF p_amount IS NULL OR p_amount < 1 OR p_amount > 100000000000 OR p_job_id IS NULL
     OR p_user_cap IS NULL OR p_user_cap < 0 OR p_user_cap > 10000000000000
     OR p_global_cap IS NULL OR p_global_cap < 0 OR p_global_cap > 10000000000000 THEN
    RAISE EXCEPTION 'invalid ai reservation' USING ERRCODE = '22023';
  END IF;
  SELECT j.lease_version INTO v_lease
    FROM public.jobs j
   WHERE j.id = p_job_id AND j.owner_id = v_owner AND j.state = 'running'
     AND j.lease_expires_at > v_now;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'ai reservation needs a running job' USING ERRCODE = '22023';
  END IF;

  INSERT INTO public.ai_spend_global (month) VALUES (v_month) ON CONFLICT (month) DO NOTHING;
  SELECT g.reserved, g.spent INTO v_g_reserved, v_g_spent
    FROM public.ai_spend_global g
   WHERE g.month = v_month
     FOR UPDATE;
  INSERT INTO public.ai_spend (owner_id, month) VALUES (v_owner, v_month)
  ON CONFLICT (owner_id, month) DO NOTHING;
  SELECT s.reserved, s.spent INTO v_u_reserved, v_u_spent
    FROM public.ai_spend s
   WHERE s.owner_id = v_owner AND s.month = v_month
     FOR UPDATE;

  IF v_g_spent + v_g_reserved + p_amount > p_global_cap
     OR v_u_spent + v_u_reserved + p_amount > p_user_cap THEN
    RETURN NULL;
  END IF;

  UPDATE public.ai_spend_global g
     SET reserved = g.reserved + p_amount, updated_at = v_now
   WHERE g.month = v_month;
  UPDATE public.ai_spend s
     SET reserved = s.reserved + p_amount, updated_at = v_now
   WHERE s.owner_id = v_owner AND s.month = v_month;
  INSERT INTO public.ai_reservations (owner_id, month, amount, job_id, lease_version)
  VALUES (v_owner, v_month, p_amount, p_job_id, v_lease)
  RETURNING id INTO v_id;
  RETURN v_id;
END
$fn$;

-- Settle one open reservation of the context user at p_actual, in the reservation's own month:
-- reserved -= amount, spent += p_actual on both rows. An actual above the amount is charged in
-- full (the caller records the overrun). True when a reservation changed; false when none was
-- open (settled, released, reaped, another user's or unknown).
CREATE OR REPLACE FUNCTION public.ai_settle(p_id uuid, p_actual bigint)
  RETURNS boolean
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  v_owner uuid := NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid;
  v_now timestamp with time zone := pg_catalog.clock_timestamp();
  v_month date;
  v_amount bigint;
BEGIN
  IF v_owner IS NULL THEN
    RAISE EXCEPTION 'no user context' USING ERRCODE = '42501';
  END IF;
  IF p_id IS NULL OR p_actual IS NULL OR p_actual < 0 OR p_actual > 1000000000000 THEN
    RAISE EXCEPTION 'invalid ai settlement' USING ERRCODE = '22023';
  END IF;
  -- The month without a lock, then the lock order: global row, user row, reservation row.
  SELECT r.month INTO v_month
    FROM public.ai_reservations r
   WHERE r.id = p_id AND r.owner_id = v_owner AND r.state = 'reserved';
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  PERFORM 1 FROM public.ai_spend_global g WHERE g.month = v_month FOR UPDATE;
  PERFORM 1 FROM public.ai_spend s WHERE s.owner_id = v_owner AND s.month = v_month FOR UPDATE;
  UPDATE public.ai_reservations r
     SET state = 'settled', actual = p_actual, ended_at = v_now
   WHERE r.id = p_id AND r.owner_id = v_owner AND r.state = 'reserved'
  RETURNING r.amount INTO v_amount;
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  UPDATE public.ai_spend_global g
     SET reserved = g.reserved - v_amount, spent = g.spent + p_actual, updated_at = v_now
   WHERE g.month = v_month;
  UPDATE public.ai_spend s
     SET reserved = s.reserved - v_amount, spent = s.spent + p_actual, updated_at = v_now
   WHERE s.owner_id = v_owner AND s.month = v_month;
  RETURN true;
END
$fn$;

-- Release one open reservation of the context user: reserved -= amount on both rows, nothing
-- spent. Only for a request that provably was not processed (ADR 0014 section 7). True when a
-- reservation changed.
CREATE OR REPLACE FUNCTION public.ai_release(p_id uuid)
  RETURNS boolean
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  v_owner uuid := NULLIF(pg_catalog.current_setting('app.user_id', true), '')::uuid;
  v_now timestamp with time zone := pg_catalog.clock_timestamp();
  v_month date;
  v_amount bigint;
BEGIN
  IF v_owner IS NULL THEN
    RAISE EXCEPTION 'no user context' USING ERRCODE = '42501';
  END IF;
  IF p_id IS NULL THEN
    RAISE EXCEPTION 'invalid ai release' USING ERRCODE = '22023';
  END IF;
  SELECT r.month INTO v_month
    FROM public.ai_reservations r
   WHERE r.id = p_id AND r.owner_id = v_owner AND r.state = 'reserved';
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  PERFORM 1 FROM public.ai_spend_global g WHERE g.month = v_month FOR UPDATE;
  PERFORM 1 FROM public.ai_spend s WHERE s.owner_id = v_owner AND s.month = v_month FOR UPDATE;
  UPDATE public.ai_reservations r
     SET state = 'released', ended_at = v_now
   WHERE r.id = p_id AND r.owner_id = v_owner AND r.state = 'reserved'
  RETURNING r.amount INTO v_amount;
  IF NOT FOUND THEN
    RETURN false;
  END IF;
  UPDATE public.ai_spend_global g
     SET reserved = g.reserved - v_amount, updated_at = v_now
   WHERE g.month = v_month;
  UPDATE public.ai_spend s
     SET reserved = s.reserved - v_amount, updated_at = v_now
   WHERE s.owner_id = v_owner AND s.month = v_month;
  RETURN true;
END
$fn$;

-- The reaper (ADR 0014 section 7): settle at their full amount, at most p_limit, the open
-- reservations whose job attempt is over: the job is gone, is not running, runs under a newer
-- lease, or its lease expired. Charged at the reserved amount because the request may have been
-- billed. Per row the same lock order and the same conditional update; returns only a count.
CREATE OR REPLACE FUNCTION public.ai_settle_lost(p_limit integer)
  RETURNS integer
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  v_now timestamp with time zone := pg_catalog.clock_timestamp();
  v_amount bigint;
  v_count integer := 0;
  r record;
BEGIN
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
    RAISE EXCEPTION 'invalid ai settle limit' USING ERRCODE = '22023';
  END IF;
  FOR r IN SELECT x.id, x.owner_id, x.month
             FROM public.ai_reservations x
             LEFT JOIN public.jobs j ON j.id = x.job_id AND j.owner_id = x.owner_id
            WHERE x.state = 'reserved'
              AND (j.id IS NULL OR j.state <> 'running' OR j.lease_version <> x.lease_version
                   OR j.lease_expires_at IS NULL OR j.lease_expires_at <= v_now)
            ORDER BY x.month, x.owner_id, x.id
            LIMIT p_limit LOOP
    PERFORM 1 FROM public.ai_spend_global g WHERE g.month = r.month FOR UPDATE;
    PERFORM 1 FROM public.ai_spend s
      WHERE s.owner_id = r.owner_id AND s.month = r.month FOR UPDATE;
    UPDATE public.ai_reservations x
       SET state = 'settled', actual = x.amount, ended_at = v_now
     WHERE x.id = r.id AND x.state = 'reserved'
    RETURNING x.amount INTO v_amount;
    IF FOUND THEN
      UPDATE public.ai_spend_global g
         SET reserved = g.reserved - v_amount, spent = g.spent + v_amount, updated_at = v_now
       WHERE g.month = r.month;
      UPDATE public.ai_spend s
         SET reserved = s.reserved - v_amount, spent = s.spent + v_amount, updated_at = v_now
       WHERE s.owner_id = r.owner_id AND s.month = r.month;
      v_count := v_count + 1;
    END IF;
  END LOOP;
  RETURN v_count;
END
$fn$;

-- The audit retention (ADR 0014 section 6): delete, at most p_limit, the ai_audit rows whose
-- created_at is more than p_months months old, oldest first. p_months is the worker's setting
-- (ai_audit_retention_months), from 12 to 120: a caller may keep rows longer, never shorter
-- than the 12 months the ADR states. Every owner's rows (account deletion cascades separately);
-- nothing else is read or deleted. Returns only a count.
CREATE OR REPLACE FUNCTION public.ai_audit_purge(p_months integer, p_limit integer)
  RETURNS integer
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  v_cutoff timestamp with time zone;
  v_count integer;
BEGIN
  IF p_months IS NULL OR p_months < 12 OR p_months > 120 THEN
    RAISE EXCEPTION 'invalid ai audit retention' USING ERRCODE = '22023';
  END IF;
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 10000 THEN
    RAISE EXCEPTION 'invalid ai audit purge limit' USING ERRCODE = '22023';
  END IF;
  v_cutoff := pg_catalog.clock_timestamp() - pg_catalog.make_interval(months => p_months);
  DELETE FROM public.ai_audit a
   WHERE a.id IN (SELECT b.id FROM public.ai_audit b
                   WHERE b.created_at < v_cutoff
                   ORDER BY b.created_at, b.id
                   LIMIT p_limit);
  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END
$fn$;

ALTER FUNCTION public.ai_reserve(bigint, uuid, bigint, bigint) OWNER TO ownvitals_ai;
ALTER FUNCTION public.ai_settle(uuid, bigint) OWNER TO ownvitals_ai;
ALTER FUNCTION public.ai_release(uuid) OWNER TO ownvitals_ai;
ALTER FUNCTION public.ai_settle_lost(integer) OWNER TO ownvitals_ai;
ALTER FUNCTION public.ai_audit_purge(integer, integer) OWNER TO ownvitals_ai;

-- Every EXECUTE but the owner's goes (PUBLIC's default, and any grant to another role), then
-- the app role gets it again.
REVOKE ALL ON FUNCTION public.ai_reserve(bigint, uuid, bigint, bigint) FROM PUBLIC, ownvitals_app;
REVOKE ALL ON FUNCTION public.ai_settle(uuid, bigint) FROM PUBLIC, ownvitals_app;
REVOKE ALL ON FUNCTION public.ai_release(uuid) FROM PUBLIC, ownvitals_app;
REVOKE ALL ON FUNCTION public.ai_settle_lost(integer) FROM PUBLIC, ownvitals_app;
REVOKE ALL ON FUNCTION public.ai_audit_purge(integer, integer) FROM PUBLIC, ownvitals_app;
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT DISTINCT p.oid::regprocedure::text AS signature,
                  a.grantee::regrole::text AS grantee, a.grantor::regrole::text AS grantor
             FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace,
                  aclexplode(p.proacl) a
            WHERE n.nspname = 'public'
              AND p.proname IN ('ai_reserve', 'ai_settle', 'ai_release', 'ai_settle_lost',
                                'ai_audit_purge')
              AND a.grantee <> 0 AND a.grantee <> p.proowner LOOP
    EXECUTE format('SET LOCAL ROLE %s', r.grantor);
    EXECUTE format('REVOKE ALL ON FUNCTION public.%s FROM %s CASCADE', r.signature, r.grantee);
    EXECUTE 'RESET ROLE';
  END LOOP;
END $$;

-- The grantor is the function owner, whether this script runs as that role or as a superuser.
GRANT EXECUTE ON FUNCTION public.ai_reserve(bigint, uuid, bigint, bigint) TO ownvitals_app;
GRANT EXECUTE ON FUNCTION public.ai_settle(uuid, bigint) TO ownvitals_app;
GRANT EXECUTE ON FUNCTION public.ai_release(uuid) TO ownvitals_app;
GRANT EXECUTE ON FUNCTION public.ai_settle_lost(integer) TO ownvitals_app;
GRANT EXECUTE ON FUNCTION public.ai_audit_purge(integer, integer) TO ownvitals_app;

COMMENT ON FUNCTION public.ai_reserve(bigint, uuid, bigint, bigint) IS 'ownvitals-ai v1';
COMMENT ON FUNCTION public.ai_settle(uuid, bigint) IS 'ownvitals-ai v1';
COMMENT ON FUNCTION public.ai_release(uuid) IS 'ownvitals-ai v1';
COMMENT ON FUNCTION public.ai_settle_lost(integer) IS 'ownvitals-ai v1';
COMMENT ON FUNCTION public.ai_audit_purge(integer, integer) IS 'ownvitals-ai v1';

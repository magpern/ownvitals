-- The mail outbox's role and SECURITY DEFINER functions (ADR 0007 sections 1 to 3, ADR 0005
-- section 3, ADR 0002 sections 3 and 5; #194).
--
-- Run as the superuser, after `alembic upgrade head` and after 20-queue.sql, in ONE transaction.
-- Plain SQL: no psql meta-commands and no transaction control. Migrations never create these
-- functions (handing a function to another role needs the superuser).
--
-- Idempotent: running it again repairs the NOLOGIN role ownvitals_mail_outbox (attributes,
-- memberships, settings), revokes and re-grants exactly its privileges, replaces the three
-- functions with their owner, ACL and version comment, and revokes the app role's privileges on
-- the system table again.
--
-- The family (ADR 0007 section 3, amended #78 and #88):
--   mail_outbox_claim(p_limit, p_lease_seconds)  claims a batch of rows from both tables;
--   mail_outbox_mark(p_id, p_outcome)            marks one claimed row sent, failed or retry;
--   owner_notice_queue(p_kind)                   writes an owner notice; the route comes from
--                                                the kind, through the fixed mapping below.
-- None takes an owner id. The claim returns for each row its id, kind, route and, for an owned
-- row, the owner_id the sender resolves the address from: never an address, never text.
--
-- The role has BYPASSRLS (the claim reads every user's owned rows) and only the column privileges
-- the three functions need on the two outbox tables: no account table, no health table.
-- 20-queue.sql admits these three functions by name and no other; their bodies are pinned by
-- digest (ownvitals/db/outbox_digests.py) and checked by web and the worker at startup.
--
-- Fixed numbers: at most 5 attempts a row; a retry waits 2 minutes times the attempts made;
-- sent and failed rows are deleted 30 days after their last change (at most 500 a table on each
-- claim); a claim takes 1 to 100 rows with a lease of 30 to 600 seconds.

DO $$
BEGIN
  IF NOT coalesce((SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user), false)
  THEN
    RAISE EXCEPTION 'this script must run as the superuser';
  END IF;
  IF pg_catalog.to_regclass('public.mail_outbox') IS NULL
     OR pg_catalog.to_regclass('public.owner_notices') IS NULL THEN
    RAISE EXCEPTION 'migrate to head first: public.mail_outbox or public.owner_notices is missing';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_app') THEN
    RAISE EXCEPTION 'ownvitals_app does not exist; run docker/postgres/init/10-roles.sql first';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_mail_outbox') THEN
    CREATE ROLE ownvitals_mail_outbox NOLOGIN;
  END IF;
END $$;

-- No login and no other power than BYPASSRLS for its own functions; a member of no role and
-- nobody a member of it; no role settings.
ALTER ROLE ownvitals_mail_outbox WITH NOLOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE
  NOREPLICATION NOINHERIT CONNECTION LIMIT -1 PASSWORD NULL;
ALTER ROLE ownvitals_mail_outbox RESET ALL;

DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT d.datname FROM pg_catalog.pg_database d
             JOIN pg_catalog.pg_db_role_setting s ON s.setdatabase = d.oid
            WHERE s.setrole = 'ownvitals_mail_outbox'::regrole LOOP
    EXECUTE format('ALTER ROLE ownvitals_mail_outbox IN DATABASE %I RESET ALL', r.datname);
  END LOOP;
  FOR r IN SELECT g.rolname AS granted, m.rolname AS member, gr.rolname AS grantor
             FROM pg_catalog.pg_auth_members am
             JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
             JOIN pg_catalog.pg_roles m ON m.oid = am.member
             JOIN pg_catalog.pg_roles gr ON gr.oid = am.grantor
            WHERE 'ownvitals_mail_outbox' IN (g.rolname, m.rolname) LOOP
    EXECUTE format('REVOKE %I FROM %I GRANTED BY %I CASCADE', r.granted, r.member, r.grantor);
  END LOOP;
END $$;

-- ownvitals_mail_outbox owns the outbox functions and nothing else, and has no default
-- privileges of its own. Anything else is not repaired automatically (dropping or reassigning it could lose
-- data): the script stops and names it.
DO $$
DECLARE
  q oid := 'ownvitals_mail_outbox'::regrole;
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
                         AND p.proname IN ('mail_outbox_claim', 'mail_outbox_mark',
                                           'owner_notice_queue')) THEN
      RAISE EXCEPTION 'ownvitals_mail_outbox owns % (catalog %, object %); it may own only the '
        'outbox functions: drop or reassign it by hand', coalesce(r.what, '?'), r.catalog, r.objid;
    END IF;
  END LOOP;
  IF EXISTS (SELECT FROM pg_catalog.pg_default_acl WHERE defaclrole = q) THEN
    RAISE EXCEPTION 'default privileges exist FOR ROLE ownvitals_mail_outbox; remove them by hand';
  END IF;
END $$;

-- Revoke every privilege ownvitals_mail_outbox holds, found through pg_shdepend (every ACL entry naming
-- a role is recorded there, for every object kind). A kind this block does not know stops the
-- script: repair it by hand rather than run with an unknown privilege.
DO $$
DECLARE
  q oid := 'ownvitals_mail_outbox'::regrole;
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
        EXECUTE format('REVOKE ALL ON %s FROM ownvitals_mail_outbox CASCADE', target);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_class' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, t.attname
                 FROM pg_catalog.pg_attribute t, aclexplode(t.attacl) a
                WHERE t.attrelid = d.objid AND t.attnum = d.objsubid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL (%I) ON TABLE %s FROM ownvitals_mail_outbox CASCADE',
                       g.attname, d.objid::regclass::text);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_namespace' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, n.nspname
                 FROM pg_catalog.pg_namespace n, aclexplode(n.nspacl) a
                WHERE n.oid = d.objid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM ownvitals_mail_outbox CASCADE', g.nspname);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_proc' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor
                 FROM pg_catalog.pg_proc p, aclexplode(p.proacl) a
                WHERE p.oid = d.objid AND a.grantee = q AND p.proowner <> q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON ROUTINE %s FROM ownvitals_mail_outbox CASCADE',
                       d.objid::regprocedure::text);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_database' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, b.datname
                 FROM pg_catalog.pg_database b, aclexplode(b.datacl) a
                WHERE b.oid = d.objid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON DATABASE %I FROM ownvitals_mail_outbox CASCADE', g.datname);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_default_acl' THEN
      FOR g IN SELECT x.defaclrole::regrole::text AS owner, x.defaclnamespace,
                      CASE x.defaclobjtype WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES'
                           WHEN 'f' THEN 'FUNCTIONS' WHEN 'T' THEN 'TYPES'
                           WHEN 'n' THEN 'SCHEMAS' END AS kind
                 FROM pg_catalog.pg_default_acl x WHERE x.oid = d.objid LOOP
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %s %s REVOKE ALL ON %s '
                       'FROM ownvitals_mail_outbox CASCADE', g.owner,
                       CASE WHEN g.defaclnamespace = 0 THEN ''
                            ELSE 'IN SCHEMA ' || quote_ident(g.defaclnamespace::regnamespace::text)
                       END, g.kind);
      END LOOP;
    ELSE
      RAISE EXCEPTION 'ownvitals_mail_outbox holds a privilege on % %; revoke it by hand',
        d.catalog, d.objid;
    END IF;
  END LOOP;
END $$;

-- Exactly these privileges (ADR 0007 section 3): USAGE on public, the claim's and the mark's
-- columns, DELETE for the retention of sent and failed rows, and INSERT of (kind, route) on the
-- system table for the owner-notice write. Everything else was revoked above.
GRANT USAGE ON SCHEMA public TO ownvitals_mail_outbox;

GRANT SELECT (id, owner_id, kind, route, state, attempts, hold_until, created_at, updated_at)
  ON TABLE public.mail_outbox TO ownvitals_mail_outbox;
GRANT UPDATE (state, attempts, hold_until, updated_at, sent_at)
  ON TABLE public.mail_outbox TO ownvitals_mail_outbox;
GRANT DELETE ON TABLE public.mail_outbox TO ownvitals_mail_outbox;

GRANT SELECT (id, kind, route, state, attempts, hold_until, created_at, updated_at)
  ON TABLE public.owner_notices TO ownvitals_mail_outbox;
GRANT INSERT (kind, route) ON TABLE public.owner_notices TO ownvitals_mail_outbox;
GRANT UPDATE (state, attempts, hold_until, updated_at, sent_at)
  ON TABLE public.owner_notices TO ownvitals_mail_outbox;
GRANT DELETE ON TABLE public.owner_notices TO ownvitals_mail_outbox;

-- The app role has no privilege on the system table (ADR 0007 section 3, amended #72); the
-- migration revokes it too, and so does 10-roles.sql's restore list.
REVOKE ALL ON TABLE public.owner_notices FROM ownvitals_app, PUBLIC;

-- Any other function under one of the three names (an overload) goes: each must exist once.
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT p.oid::regprocedure::text AS signature
             FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public'
              AND p.proname IN ('mail_outbox_claim', 'mail_outbox_mark', 'owner_notice_queue')
              AND p.oid::regprocedure::text NOT IN (
                'mail_outbox_claim(integer,integer)', 'mail_outbox_mark(uuid,text)',
                'owner_notice_queue(text)') LOOP
    EXECUTE format('DROP ROUTINE public.%s', r.signature);
  END LOOP;
END $$;

-- Claim up to p_limit rows: owner notices first, then owned rows, oldest first. A row is
-- claimable while it is pending, or claimed with an expired lease (a sender that died before
-- marking it), its hold has passed and it has attempts left. A claimed row with an expired lease
-- and no attempts left becomes failed. Locked rows are skipped, so two senders never claim the
-- same row.
CREATE OR REPLACE FUNCTION public.mail_outbox_claim(p_limit integer, p_lease_seconds integer)
  RETURNS TABLE (id uuid, kind text, route text, owner_id uuid)
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
#variable_conflict use_column
DECLARE
  v_now timestamp with time zone := pg_catalog.clock_timestamp();
  v_until timestamp with time zone;
  v_taken integer;
BEGIN
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100
     OR p_lease_seconds IS NULL OR p_lease_seconds < 30 OR p_lease_seconds > 600 THEN
    RAISE EXCEPTION 'invalid outbox claim' USING ERRCODE = '22023';
  END IF;
  v_until := v_now + pg_catalog.make_interval(secs => p_lease_seconds);

  -- Retention: sent and failed rows go 30 days after their last change.
  DELETE FROM public.owner_notices n
   WHERE n.id IN (SELECT x.id FROM public.owner_notices x
                   WHERE x.state IN ('sent', 'failed')
                     AND x.updated_at < v_now - interval '30 days'
                   LIMIT 500);
  DELETE FROM public.mail_outbox o
   WHERE o.id IN (SELECT x.id FROM public.mail_outbox x
                   WHERE x.state IN ('sent', 'failed')
                     AND x.updated_at < v_now - interval '30 days'
                   LIMIT 500);

  -- A claim that was never marked and has no attempts left.
  UPDATE public.owner_notices n
     SET state = 'failed', hold_until = NULL, updated_at = v_now
   WHERE n.state = 'claimed' AND n.hold_until <= v_now AND n.attempts >= 5;
  UPDATE public.mail_outbox o
     SET state = 'failed', hold_until = NULL, updated_at = v_now
   WHERE o.state = 'claimed' AND o.hold_until <= v_now AND o.attempts >= 5;

  RETURN QUERY
    WITH picked AS (
      SELECT x.id FROM public.owner_notices x
       WHERE x.state IN ('pending', 'claimed') AND x.attempts < 5
         AND (x.hold_until IS NULL OR x.hold_until <= v_now)
       ORDER BY x.created_at, x.id
       LIMIT p_limit
         FOR UPDATE SKIP LOCKED)
    UPDATE public.owner_notices n
       SET state = 'claimed', attempts = n.attempts + 1, hold_until = v_until, updated_at = v_now
      FROM picked
     WHERE n.id = picked.id
    RETURNING n.id, n.kind, n.route, NULL::uuid;
  GET DIAGNOSTICS v_taken = ROW_COUNT;
  IF v_taken >= p_limit THEN
    RETURN;
  END IF;

  RETURN QUERY
    WITH picked AS (
      SELECT x.id FROM public.mail_outbox x
       WHERE x.state IN ('pending', 'claimed') AND x.attempts < 5
         AND (x.hold_until IS NULL OR x.hold_until <= v_now)
       ORDER BY x.created_at, x.id
       LIMIT p_limit - v_taken
         FOR UPDATE SKIP LOCKED)
    UPDATE public.mail_outbox o
       SET state = 'claimed', attempts = o.attempts + 1, hold_until = v_until, updated_at = v_now
      FROM picked
     WHERE o.id = picked.id
    RETURNING o.id, o.kind, o.route, o.owner_id;
END
$fn$;

-- Mark one claimed row: 'sent', 'failed' (for good), or 'retry' (pending again after a hold of
-- 2 minutes times the attempts made; failed once the attempts are spent). Only a claimed row
-- changes. True when one did.
CREATE OR REPLACE FUNCTION public.mail_outbox_mark(p_id uuid, p_outcome text)
  RETURNS boolean
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  v_now timestamp with time zone := pg_catalog.clock_timestamp();
BEGIN
  IF p_id IS NULL OR p_outcome IS NULL OR p_outcome NOT IN ('sent', 'failed', 'retry') THEN
    RAISE EXCEPTION 'invalid outbox mark' USING ERRCODE = '22023';
  END IF;
  UPDATE public.owner_notices n
     SET state = CASE WHEN p_outcome = 'retry' AND n.attempts < 5 THEN 'pending'
                      WHEN p_outcome = 'sent' THEN 'sent' ELSE 'failed' END,
         hold_until = CASE WHEN p_outcome = 'retry' AND n.attempts < 5
                           THEN v_now + pg_catalog.make_interval(mins => 2 * n.attempts)
                           ELSE NULL END,
         sent_at = CASE WHEN p_outcome = 'sent' THEN v_now ELSE NULL END,
         updated_at = v_now
   WHERE n.id = p_id AND n.state = 'claimed';
  IF FOUND THEN
    RETURN true;
  END IF;
  UPDATE public.mail_outbox o
     SET state = CASE WHEN p_outcome = 'retry' AND o.attempts < 5 THEN 'pending'
                      WHEN p_outcome = 'sent' THEN 'sent' ELSE 'failed' END,
         hold_until = CASE WHEN p_outcome = 'retry' AND o.attempts < 5
                           THEN v_now + pg_catalog.make_interval(mins => 2 * o.attempts)
                           ELSE NULL END,
         sent_at = CASE WHEN p_outcome = 'sent' THEN v_now ELSE NULL END,
         updated_at = v_now
   WHERE o.id = p_id AND o.state = 'claimed';
  RETURN FOUND;
END
$fn$;

-- Write an owner notice. It takes only a kind; the route comes from this fixed mapping, one route
-- per owner-notice kind (ADR 0007 section 3, amended #88), so a caller cannot choose one. At most
-- one pending (or claimed) row per kind: a second write while one waits is coalesced into it
-- (owner_notices_one_pending). True when a row was written.
CREATE OR REPLACE FUNCTION public.owner_notice_queue(p_kind text)
  RETURNS boolean
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  v_route text;
BEGIN
  v_route := CASE p_kind
    WHEN 'connection_needs_reconnect' THEN 'admin'
    ELSE NULL END;
  IF v_route IS NULL THEN
    RAISE EXCEPTION 'unknown_kind' USING ERRCODE = '22023';
  END IF;
  INSERT INTO public.owner_notices (kind, route) VALUES (p_kind, v_route)
  ON CONFLICT (kind) WHERE state IN ('pending', 'claimed') DO NOTHING;
  RETURN FOUND;
END
$fn$;

ALTER FUNCTION public.mail_outbox_claim(integer, integer) OWNER TO ownvitals_mail_outbox;
ALTER FUNCTION public.mail_outbox_mark(uuid, text) OWNER TO ownvitals_mail_outbox;
ALTER FUNCTION public.owner_notice_queue(text) OWNER TO ownvitals_mail_outbox;

-- Every EXECUTE but the owner's goes (PUBLIC's default, and any grant to another role), then
-- the app role gets it again.
REVOKE ALL ON FUNCTION public.mail_outbox_claim(integer, integer) FROM PUBLIC, ownvitals_app;
REVOKE ALL ON FUNCTION public.mail_outbox_mark(uuid, text) FROM PUBLIC, ownvitals_app;
REVOKE ALL ON FUNCTION public.owner_notice_queue(text) FROM PUBLIC, ownvitals_app;
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT DISTINCT p.oid::regprocedure::text AS signature,
                  a.grantee::regrole::text AS grantee, a.grantor::regrole::text AS grantor
             FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace,
                  aclexplode(p.proacl) a
            WHERE n.nspname = 'public'
              AND p.proname IN ('mail_outbox_claim', 'mail_outbox_mark', 'owner_notice_queue')
              AND a.grantee <> 0 AND a.grantee <> p.proowner LOOP
    EXECUTE format('SET LOCAL ROLE %s', r.grantor);
    EXECUTE format('REVOKE ALL ON FUNCTION public.%s FROM %s CASCADE', r.signature, r.grantee);
    EXECUTE 'RESET ROLE';
  END LOOP;
END $$;

-- The grantor is the function owner, whether this script runs as that role or as a superuser.
GRANT EXECUTE ON FUNCTION public.mail_outbox_claim(integer, integer) TO ownvitals_app;
GRANT EXECUTE ON FUNCTION public.mail_outbox_mark(uuid, text) TO ownvitals_app;
GRANT EXECUTE ON FUNCTION public.owner_notice_queue(text) TO ownvitals_app;

COMMENT ON FUNCTION public.mail_outbox_claim(integer, integer) IS 'ownvitals-outbox v1';
COMMENT ON FUNCTION public.mail_outbox_mark(uuid, text) IS 'ownvitals-outbox v1';
COMMENT ON FUNCTION public.owner_notice_queue(text) IS 'ownvitals-outbox v1';

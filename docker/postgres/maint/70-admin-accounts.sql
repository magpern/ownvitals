-- The admin account family (ADR 0002 sections 3 to 5 and ADR 0005 section 3, both amended
-- 2026-11-10 by #314 and #365): admin_account_disable, admin_account_enable,
-- admin_account_delete and admin_account_revoke (#365), and the read admin_session_anomalies
-- (#461), SECURITY DEFINER functions owned by the NOLOGIN role ownvitals_admin_accounts and
-- executable by ownvitals_maint only. They let an admin's account change commit in the same
-- transaction as its ok audit row (ownvitals.admin.db.admin_session inserts that row first), and
-- the abuse view's read happen only inside its audited transaction.
--
-- Run as the superuser, after `alembic upgrade head` and after every other db-functions script
-- (30-maint.sql revokes every function privilege of ownvitals_maint, 40-withings.sql installs
-- webhook_events_purge_account and 20-queue.sql queue_enqueue_admin_revoke; this script grants
-- all three again), in ONE transaction. Plain SQL: no
-- psql meta-commands and no transaction control. Migrations never create these functions.
--
-- Idempotent: running it again repairs the role (attributes, memberships, settings), revokes and
-- re-grants exactly its privileges, and replaces the functions with their owner, ACL and version
-- comment.
--
-- Each function refuses (insufficient_privilege) unless the current transaction already holds the
-- matching ok row of admin_audit: inserted by this transaction (xmin = pg_current_xact_id()::xid,
-- created_at = now()), outcome 'ok', the function's action (user.disable, user.enable,
-- user.delete, user.revoke), target_user_id = p_target, and an actor that is an admin account
-- that is not disabled. Inside a user session (app.user_id set) it
-- raises bad_context. It then takes the target's users row FOR NO KEY UPDATE (the lock order of
-- ownvitals/auth/userlock.py), refuses an admin target, and returns an outcome: 'ok',
-- 'not_found', 'target_is_admin' or 'email_mismatch'. Nothing else leaves it: no address, no
-- hash, no token. admin_session_anomalies (#461, ADR 0017) checks the same way for its action
-- anomaly.list (target_user_id NULL) and returns rows of the abuse view only (see its comment).
--
-- The role has no BYPASSRLS: it touches system tables only (users, sessions, auth_events,
-- admin_audit). The rows that go with a deleted account are removed by the foreign keys'
-- referential actions, which Postgres runs as the referencing table's owner. Column grants only
-- where a table holds a secret: never users.password_hash, never a session token. 20-queue.sql
-- admits the five by name; their bodies are pinned by digest (ownvitals/db/queue_digests.py)
-- and checked by web and the worker at startup.

DO $$
BEGIN
  IF NOT coalesce((SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user), false)
  THEN
    RAISE EXCEPTION 'this script must run as the superuser';
  END IF;
  IF pg_catalog.to_regclass('public.users') IS NULL
     OR pg_catalog.to_regclass('public.sessions') IS NULL
     OR pg_catalog.to_regclass('public.auth_events') IS NULL
     OR pg_catalog.to_regclass('public.admin_audit') IS NULL THEN
    RAISE EXCEPTION 'migrate to head first: users, sessions, auth_events or admin_audit is missing';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_maint') THEN
    RAISE EXCEPTION 'ownvitals_maint does not exist; run docker/postgres/maint/30-maint.sql first';
  END IF;
  IF pg_catalog.to_regprocedure('public.webhook_events_purge_account(uuid)') IS NULL THEN
    RAISE EXCEPTION 'webhook_events_purge_account is missing; run '
      'docker/postgres/withings/40-withings.sql first';
  END IF;
  IF pg_catalog.to_regprocedure('public.queue_enqueue_admin_revoke(uuid,uuid,uuid)') IS NULL THEN
    RAISE EXCEPTION 'queue_enqueue_admin_revoke is missing; run '
      'docker/postgres/queue/20-queue.sql first';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_admin_accounts') THEN
    CREATE ROLE ownvitals_admin_accounts NOLOGIN;
  END IF;
END $$;

-- No login and no power at all beyond its own grants; a member of no role and nobody a member of
-- it; no role settings.
ALTER ROLE ownvitals_admin_accounts WITH NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
  NOREPLICATION NOINHERIT CONNECTION LIMIT -1 PASSWORD NULL;
ALTER ROLE ownvitals_admin_accounts RESET ALL;

DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT d.datname FROM pg_catalog.pg_database d
             JOIN pg_catalog.pg_db_role_setting s ON s.setdatabase = d.oid
            WHERE s.setrole = 'ownvitals_admin_accounts'::regrole LOOP
    EXECUTE format('ALTER ROLE ownvitals_admin_accounts IN DATABASE %I RESET ALL', r.datname);
  END LOOP;
  FOR r IN SELECT g.rolname AS granted, m.rolname AS member, gr.rolname AS grantor
             FROM pg_catalog.pg_auth_members am
             JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
             JOIN pg_catalog.pg_roles m ON m.oid = am.member
             JOIN pg_catalog.pg_roles gr ON gr.oid = am.grantor
            WHERE 'ownvitals_admin_accounts' IN (g.rolname, m.rolname) LOOP
    EXECUTE format('REVOKE %I FROM %I GRANTED BY %I CASCADE', r.granted, r.member, r.grantor);
  END LOOP;
END $$;

-- ownvitals_admin_accounts owns the four functions and nothing else, and has no default
-- privileges of its own. Anything else is not repaired automatically (dropping or reassigning it
-- could lose data): the script stops and names it.
DO $$
DECLARE
  q oid := 'ownvitals_admin_accounts'::regrole;
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
                         AND p.proname IN ('admin_account_disable', 'admin_account_enable',
                                           'admin_account_delete', 'admin_account_revoke',
                                           'admin_session_anomalies')) THEN
      RAISE EXCEPTION 'ownvitals_admin_accounts owns % (catalog %, object %); it may own only '
        'the admin account functions: drop or reassign it by hand',
        coalesce(r.what, '?'), r.catalog, r.objid;
    END IF;
  END LOOP;
  IF EXISTS (SELECT FROM pg_catalog.pg_default_acl WHERE defaclrole = q) THEN
    RAISE EXCEPTION 'default privileges exist FOR ROLE ownvitals_admin_accounts; remove them by '
      'hand';
  END IF;
END $$;

-- Revoke every privilege ownvitals_admin_accounts holds, found through pg_shdepend (every ACL
-- entry naming a role is recorded there, for every object kind). A kind this block does not know
-- stops the script: repair it by hand rather than run with an unknown privilege.
DO $$
DECLARE
  q oid := 'ownvitals_admin_accounts'::regrole;
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
        EXECUTE format('REVOKE ALL ON %s FROM ownvitals_admin_accounts CASCADE', target);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_class' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, t.attname
                 FROM pg_catalog.pg_attribute t, aclexplode(t.attacl) a
                WHERE t.attrelid = d.objid AND t.attnum = d.objsubid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL (%I) ON TABLE %s FROM ownvitals_admin_accounts CASCADE',
                       g.attname, d.objid::regclass::text);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_namespace' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, n.nspname
                 FROM pg_catalog.pg_namespace n, aclexplode(n.nspacl) a
                WHERE n.oid = d.objid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM ownvitals_admin_accounts CASCADE',
                       g.nspname);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_proc' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor
                 FROM pg_catalog.pg_proc p, aclexplode(p.proacl) a
                WHERE p.oid = d.objid AND a.grantee = q AND p.proowner <> q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON ROUTINE %s FROM ownvitals_admin_accounts CASCADE',
                       d.objid::regprocedure::text);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_database' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, b.datname
                 FROM pg_catalog.pg_database b, aclexplode(b.datacl) a
                WHERE b.oid = d.objid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON DATABASE %I FROM ownvitals_admin_accounts CASCADE',
                       g.datname);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_default_acl' THEN
      FOR g IN SELECT x.defaclrole::regrole::text AS owner, x.defaclnamespace,
                      CASE x.defaclobjtype WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES'
                           WHEN 'f' THEN 'FUNCTIONS' WHEN 'T' THEN 'TYPES'
                           WHEN 'n' THEN 'SCHEMAS' END AS kind
                 FROM pg_catalog.pg_default_acl x WHERE x.oid = d.objid LOOP
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %s %s REVOKE ALL ON %s '
                       'FROM ownvitals_admin_accounts CASCADE', g.owner,
                       CASE WHEN g.defaclnamespace = 0 THEN ''
                            ELSE 'IN SCHEMA ' || quote_ident(g.defaclnamespace::regnamespace::text)
                       END, g.kind);
      END LOOP;
    ELSE
      RAISE EXCEPTION 'ownvitals_admin_accounts holds a privilege on % %; revoke it by hand',
        d.catalog, d.objid;
    END IF;
  END LOOP;
END $$;

-- Exactly these privileges (ADR 0002 section 3). Everything else was revoked above.
GRANT USAGE ON SCHEMA public TO ownvitals_admin_accounts;
-- The target's row: its role and address for the checks, disabled_at for disable and enable,
-- the row itself for delete. Never password_hash.
GRANT SELECT (id, role, email_normalized, disabled_at) ON TABLE public.users
  TO ownvitals_admin_accounts;
GRANT UPDATE (disabled_at) ON TABLE public.users TO ownvitals_admin_accounts;
GRANT DELETE ON TABLE public.users TO ownvitals_admin_accounts;
-- Disable signs the target out everywhere. Never the token hash, address or user agent.
GRANT SELECT (user_id) ON TABLE public.sessions TO ownvitals_admin_accounts;
GRANT DELETE ON TABLE public.sessions TO ownvitals_admin_accounts;
-- The account_deleted event, with no user_id (the row is about to go).
GRANT INSERT (kind, ip, browser, os, place) ON TABLE public.auth_events
  TO ownvitals_admin_accounts;
-- The abuse view (#461): the session_anomaly rows, never the address (ip) of any row.
GRANT SELECT (id, user_id, kind, at, detail, browser, os, place) ON TABLE public.auth_events
  TO ownvitals_admin_accounts;
-- The check that this transaction holds the matching ok audit row. Never the IP's HMAC, never
-- a write: the log stays append-only through maint alone.
GRANT SELECT (xmin, created_at, actor_id, action, target_user_id, outcome)
  ON TABLE public.admin_audit TO ownvitals_admin_accounts;
-- The delete purges the target's webhook events (ADR 0002 section 3, #362).
GRANT EXECUTE ON FUNCTION public.webhook_events_purge_account(uuid) TO ownvitals_admin_accounts;
-- The revoke queues the target's withings_admin_revoke job (ADR 0002 section 3, #365): the only
-- grantee of that queue function.
GRANT EXECUTE ON FUNCTION public.queue_enqueue_admin_revoke(uuid, uuid, uuid)
  TO ownvitals_admin_accounts;

-- Any other function under these names (an overload) goes: each must exist once.
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT p.oid::regprocedure::text AS signature
             FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public'
              AND p.proname IN ('admin_account_disable', 'admin_account_enable',
                                'admin_account_delete', 'admin_account_revoke',
                                'admin_session_anomalies')
              AND p.oid::regprocedure::text NOT IN (
                'admin_account_disable(uuid)', 'admin_account_enable(uuid)',
                'admin_account_delete(uuid,text,text,text,text,text)',
                'admin_account_revoke(uuid,text,uuid)',
                'admin_session_anomalies(integer,timestamp with time zone,uuid)') LOOP
    EXECUTE format('DROP ROUTINE public.%s', r.signature);
  END LOOP;
  -- An earlier draft with other OUT columns cannot be replaced in place: it goes first.
  IF EXISTS (SELECT FROM pg_catalog.pg_proc p
               JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname = 'public' AND p.proname = 'admin_account_disable'
                AND pg_catalog.pg_get_function_identity_arguments(p.oid)
                    <> 'p_target uuid, OUT outcome text, OUT disabled_at timestamp with time '
                       'zone, OUT sessions_revoked integer') THEN
    DROP FUNCTION public.admin_account_disable(uuid);
  END IF;
  -- Likewise an earlier draft of the abuse view's read with other result columns.
  IF EXISTS (SELECT FROM pg_catalog.pg_proc p
               JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname = 'public' AND p.proname = 'admin_session_anomalies'
                AND pg_catalog.pg_get_function_result(p.oid)
                    <> 'TABLE(id uuid, at timestamp with time zone, user_id uuid, email text, '
                       'signals jsonb, browser text, os text, place text)') THEN
    DROP FUNCTION public.admin_session_anomalies(integer, timestamp with time zone, uuid);
  END IF;
END $$;

-- Disable: disabled_at (an earlier value is kept) and every session of the target deleted.
CREATE OR REPLACE FUNCTION public.admin_account_disable(
  p_target uuid, OUT outcome text, OUT disabled_at timestamp with time zone,
  OUT sessions_revoked integer)
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  v_role text;
BEGIN
  IF coalesce(pg_catalog.current_setting('app.user_id', true), '') <> '' THEN
    RAISE EXCEPTION 'bad_context' USING ERRCODE = '42501';
  END IF;
  IF p_target IS NULL OR NOT EXISTS (
       SELECT FROM public.admin_audit a JOIN public.users u ON u.id = a.actor_id
        WHERE a.xmin = pg_catalog.pg_current_xact_id()::xid
          AND a.created_at = pg_catalog.now() AND a.outcome = 'ok'
          AND a.action = 'user.disable' AND a.target_user_id = p_target
          AND u.role = 'admin' AND u.disabled_at IS NULL) THEN
    RAISE EXCEPTION 'no_audit_row' USING ERRCODE = '42501';
  END IF;
  SELECT u.role INTO v_role FROM public.users u WHERE u.id = p_target FOR NO KEY UPDATE;
  IF NOT FOUND THEN
    outcome := 'not_found';
    RETURN;
  END IF;
  IF v_role = 'admin' THEN
    outcome := 'target_is_admin';
    RETURN;
  END IF;
  UPDATE public.users u SET disabled_at = coalesce(u.disabled_at, pg_catalog.now())
   WHERE u.id = p_target
  RETURNING u.disabled_at INTO disabled_at;
  DELETE FROM public.sessions s WHERE s.user_id = p_target;
  GET DIAGNOSTICS sessions_revoked = ROW_COUNT;
  outcome := 'ok';
END
$fn$;

-- Enable: disabled_at cleared.
CREATE OR REPLACE FUNCTION public.admin_account_enable(p_target uuid)
  RETURNS text
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  v_role text;
BEGIN
  IF coalesce(pg_catalog.current_setting('app.user_id', true), '') <> '' THEN
    RAISE EXCEPTION 'bad_context' USING ERRCODE = '42501';
  END IF;
  IF p_target IS NULL OR NOT EXISTS (
       SELECT FROM public.admin_audit a JOIN public.users u ON u.id = a.actor_id
        WHERE a.xmin = pg_catalog.pg_current_xact_id()::xid
          AND a.created_at = pg_catalog.now() AND a.outcome = 'ok'
          AND a.action = 'user.enable' AND a.target_user_id = p_target
          AND u.role = 'admin' AND u.disabled_at IS NULL) THEN
    RAISE EXCEPTION 'no_audit_row' USING ERRCODE = '42501';
  END IF;
  SELECT u.role INTO v_role FROM public.users u WHERE u.id = p_target FOR NO KEY UPDATE;
  IF NOT FOUND THEN
    RETURN 'not_found';
  END IF;
  IF v_role = 'admin' THEN
    RETURN 'target_is_admin';
  END IF;
  UPDATE public.users u SET disabled_at = NULL WHERE u.id = p_target;
  RETURN 'ok';
END
$fn$;

-- Delete: the steps of the user's own account delete. The target's webhook events first, while
-- its connection rows still name them (#362); the account_deleted event, without a user_id,
-- with the admin request's address and origin; then the users row, which every owned row
-- follows by its foreign key.
CREATE OR REPLACE FUNCTION public.admin_account_delete(
  p_target uuid, p_email_normalized text, p_ip text, p_browser text, p_os text, p_place text)
  RETURNS text
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  v_role text;
  v_email text;
BEGIN
  IF coalesce(pg_catalog.current_setting('app.user_id', true), '') <> '' THEN
    RAISE EXCEPTION 'bad_context' USING ERRCODE = '42501';
  END IF;
  IF p_target IS NULL OR NOT EXISTS (
       SELECT FROM public.admin_audit a JOIN public.users u ON u.id = a.actor_id
        WHERE a.xmin = pg_catalog.pg_current_xact_id()::xid
          AND a.created_at = pg_catalog.now() AND a.outcome = 'ok'
          AND a.action = 'user.delete' AND a.target_user_id = p_target
          AND u.role = 'admin' AND u.disabled_at IS NULL) THEN
    RAISE EXCEPTION 'no_audit_row' USING ERRCODE = '42501';
  END IF;
  SELECT u.role, u.email_normalized INTO v_role, v_email
    FROM public.users u WHERE u.id = p_target FOR NO KEY UPDATE;
  IF NOT FOUND THEN
    RETURN 'not_found';
  END IF;
  IF v_role = 'admin' THEN
    RETURN 'target_is_admin';
  END IF;
  IF p_email_normalized IS NULL OR v_email <> p_email_normalized THEN
    RETURN 'email_mismatch';
  END IF;
  PERFORM public.webhook_events_purge_account(p_target);
  INSERT INTO public.auth_events (kind, ip, browser, os, place)
  VALUES ('account_deleted', p_ip, p_browser, p_os, p_place);
  DELETE FROM public.users u WHERE u.id = p_target;
  RETURN 'ok';
END
$fn$;

-- Revoke (#365): the delete's first step, in its own audited transaction (action user.revoke)
-- that commits before the delete's, so the worker can claim the job. The delete's checks first
-- (the target exists, is no admin and has this address), so a refused delete never revokes;
-- then one withings_admin_revoke job for the target, naming the audit row's actor (never an id
-- from the caller) and the admin's session, which the job's handler checks again. job_id is
-- NULL when the target has no live connection. Nothing else changes here.
CREATE OR REPLACE FUNCTION public.admin_account_revoke(
  p_target uuid, p_email_normalized text, p_session uuid, OUT outcome text, OUT job_id uuid)
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  v_actor uuid;
  v_role text;
  v_email text;
BEGIN
  IF coalesce(pg_catalog.current_setting('app.user_id', true), '') <> '' THEN
    RAISE EXCEPTION 'bad_context' USING ERRCODE = '42501';
  END IF;
  SELECT a.actor_id INTO v_actor
    FROM public.admin_audit a JOIN public.users u ON u.id = a.actor_id
   WHERE a.xmin = pg_catalog.pg_current_xact_id()::xid
     AND a.created_at = pg_catalog.now() AND a.outcome = 'ok'
     AND a.action = 'user.revoke' AND a.target_user_id = p_target
     AND u.role = 'admin' AND u.disabled_at IS NULL
   LIMIT 1;
  IF p_target IS NULL OR p_session IS NULL OR v_actor IS NULL THEN
    RAISE EXCEPTION 'no_audit_row' USING ERRCODE = '42501';
  END IF;
  SELECT u.role, u.email_normalized INTO v_role, v_email
    FROM public.users u WHERE u.id = p_target FOR NO KEY UPDATE;
  IF NOT FOUND THEN
    outcome := 'not_found';
    RETURN;
  END IF;
  IF v_role = 'admin' THEN
    outcome := 'target_is_admin';
    RETURN;
  END IF;
  IF p_email_normalized IS NULL OR v_email <> p_email_normalized THEN
    outcome := 'email_mismatch';
    RETURN;
  END IF;
  job_id := public.queue_enqueue_admin_revoke(p_target, v_actor, p_session);
  outcome := 'ok';
END
$fn$;

-- The admin abuse view (#461, ADR 0017): the session_anomaly events of the last 30 days across
-- every user, newest first, at most p_limit (1 to 101) of them, after the cursor
-- (p_before_at, p_before_id) when one is given (both or neither). Only inside the transaction
-- that holds this admin's ok anomaly.list row, as the account functions are. Each row: the
-- event's id and time, the account's id and address (NULL once the account is gone), the
-- event's signal codes (detail -> 'signals') and its coarse origin. Never the event's address,
-- any other detail, a session, a token or another kind of event.
CREATE OR REPLACE FUNCTION public.admin_session_anomalies(
  p_limit integer, p_before_at timestamp with time zone, p_before_id uuid)
  RETURNS TABLE(id uuid, at timestamp with time zone, user_id uuid, email text, signals jsonb,
                browser text, os text, place text)
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
BEGIN
  IF coalesce(pg_catalog.current_setting('app.user_id', true), '') <> '' THEN
    RAISE EXCEPTION 'bad_context' USING ERRCODE = '42501';
  END IF;
  IF NOT EXISTS (
       SELECT FROM public.admin_audit a JOIN public.users u ON u.id = a.actor_id
        WHERE a.xmin = pg_catalog.pg_current_xact_id()::xid
          AND a.created_at = pg_catalog.now() AND a.outcome = 'ok'
          AND a.action = 'anomaly.list' AND a.target_user_id IS NULL
          AND u.role = 'admin' AND u.disabled_at IS NULL) THEN
    RAISE EXCEPTION 'no_audit_row' USING ERRCODE = '42501';
  END IF;
  IF p_limit IS NULL OR p_limit < 1 OR p_limit > 101
     OR (p_before_at IS NULL) <> (p_before_id IS NULL) THEN
    RAISE EXCEPTION 'bad_argument' USING ERRCODE = '22023';
  END IF;
  RETURN QUERY
    SELECT e.id, e.at, e.user_id, u.email_normalized, e.detail -> 'signals',
           e.browser, e.os, e.place
      FROM public.auth_events e LEFT JOIN public.users u ON u.id = e.user_id
     WHERE e.kind = 'session_anomaly'
       AND e.at > pg_catalog.now() - interval '30 days'
       AND (p_before_at IS NULL OR (e.at, e.id) < (p_before_at, p_before_id))
     ORDER BY e.at DESC, e.id DESC
     LIMIT p_limit;
END
$fn$;

ALTER FUNCTION public.admin_account_disable(uuid) OWNER TO ownvitals_admin_accounts;
ALTER FUNCTION public.admin_account_enable(uuid) OWNER TO ownvitals_admin_accounts;
ALTER FUNCTION public.admin_account_delete(uuid, text, text, text, text, text)
  OWNER TO ownvitals_admin_accounts;
ALTER FUNCTION public.admin_account_revoke(uuid, text, uuid) OWNER TO ownvitals_admin_accounts;
ALTER FUNCTION public.admin_session_anomalies(integer, timestamp with time zone, uuid)
  OWNER TO ownvitals_admin_accounts;

-- Every EXECUTE but the owner's goes (PUBLIC's default, and any grant to another role), then
-- the admin role gets it again.
REVOKE ALL ON FUNCTION public.admin_account_disable(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.admin_account_enable(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.admin_account_delete(uuid, text, text, text, text, text)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION public.admin_account_revoke(uuid, text, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.admin_session_anomalies(integer, timestamp with time zone, uuid)
  FROM PUBLIC;
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT DISTINCT p.oid::regprocedure::text AS signature,
                  a.grantee::regrole::text AS grantee, a.grantor::regrole::text AS grantor
             FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace,
                  aclexplode(p.proacl) a
            WHERE n.nspname = 'public'
              AND p.proname IN ('admin_account_disable', 'admin_account_enable',
                                'admin_account_delete', 'admin_account_revoke',
                                'admin_session_anomalies')
              AND a.grantee <> 0 AND a.grantee <> p.proowner LOOP
    EXECUTE format('SET LOCAL ROLE %s', r.grantor);
    EXECUTE format('REVOKE ALL ON FUNCTION public.%s FROM %s CASCADE', r.signature, r.grantee);
    EXECUTE 'RESET ROLE';
  END LOOP;
END $$;

-- The grantor is the function owner, whether this script runs as that role or as a superuser.
GRANT EXECUTE ON FUNCTION public.admin_account_disable(uuid) TO ownvitals_maint;
GRANT EXECUTE ON FUNCTION public.admin_account_enable(uuid) TO ownvitals_maint;
GRANT EXECUTE ON FUNCTION public.admin_account_delete(uuid, text, text, text, text, text)
  TO ownvitals_maint;
GRANT EXECUTE ON FUNCTION public.admin_account_revoke(uuid, text, uuid) TO ownvitals_maint;
GRANT EXECUTE ON FUNCTION public.admin_session_anomalies(integer, timestamp with time zone, uuid)
  TO ownvitals_maint;

COMMENT ON FUNCTION public.admin_account_disable(uuid) IS 'ownvitals-admin-accounts v1';
COMMENT ON FUNCTION public.admin_account_enable(uuid) IS 'ownvitals-admin-accounts v1';
COMMENT ON FUNCTION public.admin_account_delete(uuid, text, text, text, text, text)
  IS 'ownvitals-admin-accounts v1';
COMMENT ON FUNCTION public.admin_account_revoke(uuid, text, uuid) IS 'ownvitals-admin-accounts v1';
COMMENT ON FUNCTION public.admin_session_anomalies(integer, timestamp with time zone, uuid)
  IS 'ownvitals-admin-accounts v1';

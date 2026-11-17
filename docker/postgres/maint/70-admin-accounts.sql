-- The admin account family (ADR 0002 sections 3 to 5 and ADR 0005 section 3, both amended
-- 2026-11-10 by #314 and #365): admin_account_disable, admin_account_enable,
-- admin_account_delete and admin_account_revoke (#365), and the read admin_session_anomalies
-- (#461), with the admin page's writes admin_account_signout and admin_invite_create (issue
-- 550), SECURITY DEFINER functions owned by the NOLOGIN role ownvitals_admin_accounts and
-- executable by ownvitals_maint only. They let an admin's account change commit in the same
-- transaction as its ok audit row (ownvitals.admin.db.admin_session inserts that row first), and
-- the abuse view's read happen only inside its audited transaction. The script's last section
-- (issue 549) adds the two admin connection-state reads, admin_withings_states and
-- admin_withings_counts, owned by a second NOLOGIN role, ownvitals_admin_connections (see there).
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
-- hash, no token. admin_session_anomalies (#461, ADR 0020) checks the same way for its action
-- anomaly.list (target_user_id NULL) and returns rows of the abuse view only (see its comment).
-- admin_account_signout (sessions.revoke_all) and admin_invite_create (invite.create, target
-- NULL) check the same way; see their comments for what each changes and returns.
--
-- The role has no BYPASSRLS: it touches system tables only (users, sessions, auth_events,
-- invites, admin_audit). The rows that go with a deleted account are removed by the foreign keys'
-- referential actions, which Postgres runs as the referencing table's owner. Column grants only
-- where a table holds a secret: never users.password_hash, never a session token, and only an
-- insert into invites (never a read of a code hash). 20-queue.sql admits the seven by name; their bodies are pinned by digest (ownvitals/db/queue_digests.py)
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
     OR pg_catalog.to_regclass('public.invites') IS NULL
     OR pg_catalog.to_regclass('public.admin_audit') IS NULL THEN
    RAISE EXCEPTION 'migrate to head first: users, sessions, auth_events, invites or admin_audit '
      'is missing';
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

-- ownvitals_admin_accounts owns the seven functions and nothing else, and has no default
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
                                           'admin_session_anomalies', 'admin_account_signout',
                                           'admin_invite_create')) THEN
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
-- An admin's invite (issue 550): the two columns the operator CLI's invite stores, insert only.
-- Never a read of invites, so never a code hash out of it.
GRANT INSERT (code_hash, expires_at) ON TABLE public.invites TO ownvitals_admin_accounts;

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
                                'admin_session_anomalies', 'admin_account_signout',
                                'admin_invite_create')
              AND p.oid::regprocedure::text NOT IN (
                'admin_account_disable(uuid)', 'admin_account_enable(uuid)',
                'admin_account_delete(uuid,text,text,text,text,text)',
                'admin_account_revoke(uuid,text,uuid)',
                'admin_session_anomalies(integer,timestamp with time zone,uuid)',
                'admin_account_signout(uuid)', 'admin_invite_create(bytea,interval)') LOOP
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

-- The admin abuse view (#461, ADR 0020): the session_anomaly events of the last 30 days across
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

-- Sign out all (issue 550): every session of the target deleted, nothing else changed. Unlike
-- disable it takes an admin target too, the acting admin's own account included (the mock-up's
-- "Sign out all..." is on every account with a session): it leaves no lasting state, the account
-- signs in again as before, and the admin role itself stays with the operator CLI. Returns
-- 'not_found' (and 0) for an unknown target, else 'ok' and how many sessions went.
CREATE OR REPLACE FUNCTION public.admin_account_signout(
  p_target uuid, OUT outcome text, OUT sessions_revoked integer)
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
BEGIN
  IF coalesce(pg_catalog.current_setting('app.user_id', true), '') <> '' THEN
    RAISE EXCEPTION 'bad_context' USING ERRCODE = '42501';
  END IF;
  IF p_target IS NULL OR NOT EXISTS (
       SELECT FROM public.admin_audit a JOIN public.users u ON u.id = a.actor_id
        WHERE a.xmin = pg_catalog.pg_current_xact_id()::xid
          AND a.created_at = pg_catalog.now() AND a.outcome = 'ok'
          AND a.action = 'sessions.revoke_all' AND a.target_user_id = p_target
          AND u.role = 'admin' AND u.disabled_at IS NULL) THEN
    RAISE EXCEPTION 'no_audit_row' USING ERRCODE = '42501';
  END IF;
  sessions_revoked := 0;
  PERFORM FROM public.users u WHERE u.id = p_target FOR NO KEY UPDATE;
  IF NOT FOUND THEN
    outcome := 'not_found';
    RETURN;
  END IF;
  DELETE FROM public.sessions s WHERE s.user_id = p_target;
  GET DIAGNOSTICS sessions_revoked = ROW_COUNT;
  outcome := 'ok';
END
$fn$;

-- Invite (issue 550): one invite row as the operator CLI's `invite create` stores it, the code's
-- SHA-256 and the expiry and nothing else (created_by stays NULL: the audit row names the admin).
-- The code itself never reaches the database. Refuses (bad_argument) a hash that is not 32 bytes
-- and an expiry outside one hour to 30 days. Returns the expiry; never a row id or a hash.
CREATE OR REPLACE FUNCTION public.admin_invite_create(p_code_hash bytea, p_expires_in interval)
  RETURNS timestamp with time zone
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  v_expires timestamp with time zone;
BEGIN
  IF coalesce(pg_catalog.current_setting('app.user_id', true), '') <> '' THEN
    RAISE EXCEPTION 'bad_context' USING ERRCODE = '42501';
  END IF;
  IF NOT EXISTS (
       SELECT FROM public.admin_audit a JOIN public.users u ON u.id = a.actor_id
        WHERE a.xmin = pg_catalog.pg_current_xact_id()::xid
          AND a.created_at = pg_catalog.now() AND a.outcome = 'ok'
          AND a.action = 'invite.create' AND a.target_user_id IS NULL
          AND u.role = 'admin' AND u.disabled_at IS NULL) THEN
    RAISE EXCEPTION 'no_audit_row' USING ERRCODE = '42501';
  END IF;
  IF p_code_hash IS NULL OR pg_catalog.octet_length(p_code_hash) <> 32
     OR p_expires_in IS NULL OR p_expires_in < interval '1 hour'
     OR p_expires_in > interval '30 days' THEN
    RAISE EXCEPTION 'bad_argument' USING ERRCODE = '22023';
  END IF;
  v_expires := pg_catalog.now() + p_expires_in;
  INSERT INTO public.invites (code_hash, expires_at) VALUES (p_code_hash, v_expires);
  RETURN v_expires;
END
$fn$;

ALTER FUNCTION public.admin_account_disable(uuid) OWNER TO ownvitals_admin_accounts;
ALTER FUNCTION public.admin_account_enable(uuid) OWNER TO ownvitals_admin_accounts;
ALTER FUNCTION public.admin_account_delete(uuid, text, text, text, text, text)
  OWNER TO ownvitals_admin_accounts;
ALTER FUNCTION public.admin_account_revoke(uuid, text, uuid) OWNER TO ownvitals_admin_accounts;
ALTER FUNCTION public.admin_session_anomalies(integer, timestamp with time zone, uuid)
  OWNER TO ownvitals_admin_accounts;
ALTER FUNCTION public.admin_account_signout(uuid) OWNER TO ownvitals_admin_accounts;
ALTER FUNCTION public.admin_invite_create(bytea, interval) OWNER TO ownvitals_admin_accounts;

-- Every EXECUTE but the owner's goes (PUBLIC's default, and any grant to another role), then
-- the admin role gets it again.
REVOKE ALL ON FUNCTION public.admin_account_disable(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.admin_account_enable(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.admin_account_delete(uuid, text, text, text, text, text)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION public.admin_account_revoke(uuid, text, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.admin_session_anomalies(integer, timestamp with time zone, uuid)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION public.admin_account_signout(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.admin_invite_create(bytea, interval) FROM PUBLIC;
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
                                'admin_session_anomalies', 'admin_account_signout',
                                'admin_invite_create')
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
GRANT EXECUTE ON FUNCTION public.admin_account_signout(uuid) TO ownvitals_maint;
GRANT EXECUTE ON FUNCTION public.admin_invite_create(bytea, interval) TO ownvitals_maint;

COMMENT ON FUNCTION public.admin_account_disable(uuid) IS 'ownvitals-admin-accounts v1';
COMMENT ON FUNCTION public.admin_account_enable(uuid) IS 'ownvitals-admin-accounts v1';
COMMENT ON FUNCTION public.admin_account_delete(uuid, text, text, text, text, text)
  IS 'ownvitals-admin-accounts v1';
COMMENT ON FUNCTION public.admin_account_revoke(uuid, text, uuid) IS 'ownvitals-admin-accounts v1';
COMMENT ON FUNCTION public.admin_session_anomalies(integer, timestamp with time zone, uuid)
  IS 'ownvitals-admin-accounts v1';
COMMENT ON FUNCTION public.admin_account_signout(uuid) IS 'ownvitals-admin-accounts v1';
COMMENT ON FUNCTION public.admin_invite_create(bytea, interval) IS 'ownvitals-admin-accounts v1';

-- ===============================================================================================
-- The admin connection-state reads (issue 549; ADR 0002 sections 3 and 4): admin_withings_states
-- (the Withings column of the Users view) and admin_withings_counts (the Overview's connection
-- tile), SECURITY DEFINER functions owned by the NOLOGIN role ownvitals_admin_connections and
-- executable by ownvitals_maint only. Maint itself reads no column of connections (guard 11): these
-- two are the only way the admin area learns a connection's state, and what leaves them is an
-- account id the caller passed in with one of three words (healthy, needs_reconnect,
-- not_connected), or the three words with a count. Never a credential, a Withings user id, an
-- app id, an error, a time or anything of a health table.
--
-- The words are the user's own Connections page's (ownvitals.views.states.stream_state): no live
-- Withings row is not_connected, needs_reconnect is needs_reconnect, and connected is the design's
-- "Healthy". A disconnected row is not live. The test provider's rows are not Withings and count
-- as not connected.
--
-- Each refuses (insufficient_privilege) unless the current transaction already holds its ok row
-- of admin_audit, checked as admin_session_anomalies checks its own: admin_withings_states for
-- user.list (target_user_id NULL) or for user.read of the one account it is asked about,
-- admin_withings_counts for overview.read. Inside a user session (app.user_id set) both raise
-- bad_context.
--
-- The role needs BYPASSRLS (connections is under forced row security) and holds exactly: SELECT
-- (owner_id, provider, status) on connections, (id, role, disabled_at) on users, and the audit
-- check's columns of admin_audit. Same repair as ownvitals_admin_accounts above.

DO $$
BEGIN
  IF pg_catalog.to_regclass('public.connections') IS NULL THEN
    RAISE EXCEPTION 'migrate to head first: connections is missing';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_admin_connections')
  THEN
    CREATE ROLE ownvitals_admin_connections NOLOGIN;
  END IF;
END $$;

-- No login and no power beyond its own grants, but BYPASSRLS: connections is an owned table under
-- forced row security, and this role reads three of its columns for every account. A member of
-- no role and nobody a member of it; no role settings.
ALTER ROLE ownvitals_admin_connections WITH NOLOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE
  NOREPLICATION NOINHERIT CONNECTION LIMIT -1 PASSWORD NULL;
ALTER ROLE ownvitals_admin_connections RESET ALL;

DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT d.datname FROM pg_catalog.pg_database d
             JOIN pg_catalog.pg_db_role_setting s ON s.setdatabase = d.oid
            WHERE s.setrole = 'ownvitals_admin_connections'::regrole LOOP
    EXECUTE format('ALTER ROLE ownvitals_admin_connections IN DATABASE %I RESET ALL', r.datname);
  END LOOP;
  FOR r IN SELECT g.rolname AS granted, m.rolname AS member, gr.rolname AS grantor
             FROM pg_catalog.pg_auth_members am
             JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
             JOIN pg_catalog.pg_roles m ON m.oid = am.member
             JOIN pg_catalog.pg_roles gr ON gr.oid = am.grantor
            WHERE 'ownvitals_admin_connections' IN (g.rolname, m.rolname) LOOP
    EXECUTE format('REVOKE %I FROM %I GRANTED BY %I CASCADE', r.granted, r.member, r.grantor);
  END LOOP;
END $$;

-- ownvitals_admin_connections owns the two connection-state functions and nothing else, and has
-- no default privileges of its own. Anything else is not repaired automatically (dropping or reassigning it
-- could lose data): the script stops and names it.
DO $$
DECLARE
  q oid := 'ownvitals_admin_connections'::regrole;
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
                         AND p.proname IN ('admin_withings_states', 'admin_withings_counts')) THEN
      RAISE EXCEPTION 'ownvitals_admin_connections owns % (catalog %, object %); it may own only '
        'the admin connection-state functions: drop or reassign it by hand',
        coalesce(r.what, '?'), r.catalog, r.objid;
    END IF;
  END LOOP;
  IF EXISTS (SELECT FROM pg_catalog.pg_default_acl WHERE defaclrole = q) THEN
    RAISE EXCEPTION 'default privileges exist FOR ROLE ownvitals_admin_connections; remove them by '
      'hand';
  END IF;
END $$;

-- Revoke every privilege ownvitals_admin_connections holds, found through pg_shdepend (every ACL
-- entry naming a role is recorded there, for every object kind). A kind this block does not know
-- stops the script: repair it by hand rather than run with an unknown privilege.
DO $$
DECLARE
  q oid := 'ownvitals_admin_connections'::regrole;
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
        EXECUTE format('REVOKE ALL ON %s FROM ownvitals_admin_connections CASCADE', target);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_class' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, t.attname
                 FROM pg_catalog.pg_attribute t, aclexplode(t.attacl) a
                WHERE t.attrelid = d.objid AND t.attnum = d.objsubid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL (%I) ON TABLE %s FROM ownvitals_admin_connections CASCADE',
                       g.attname, d.objid::regclass::text);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_namespace' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, n.nspname
                 FROM pg_catalog.pg_namespace n, aclexplode(n.nspacl) a
                WHERE n.oid = d.objid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM ownvitals_admin_connections CASCADE',
                       g.nspname);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_proc' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor
                 FROM pg_catalog.pg_proc p, aclexplode(p.proacl) a
                WHERE p.oid = d.objid AND a.grantee = q AND p.proowner <> q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON ROUTINE %s FROM ownvitals_admin_connections CASCADE',
                       d.objid::regprocedure::text);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_database' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, b.datname
                 FROM pg_catalog.pg_database b, aclexplode(b.datacl) a
                WHERE b.oid = d.objid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON DATABASE %I FROM ownvitals_admin_connections CASCADE',
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
                       'FROM ownvitals_admin_connections CASCADE', g.owner,
                       CASE WHEN g.defaclnamespace = 0 THEN ''
                            ELSE 'IN SCHEMA ' || quote_ident(g.defaclnamespace::regnamespace::text)
                       END, g.kind);
      END LOOP;
    ELSE
      RAISE EXCEPTION 'ownvitals_admin_connections holds a privilege on % %; revoke it by hand',
        d.catalog, d.objid;
    END IF;
  END LOOP;
END $$;

-- Exactly these privileges. Everything else was revoked above.
GRANT USAGE ON SCHEMA public TO ownvitals_admin_connections;
-- A connection's owner, provider and state. Never a credential column, public, app_id,
-- withings_userid, last_error or a time.
GRANT SELECT (owner_id, provider, status) ON TABLE public.connections
  TO ownvitals_admin_connections;
-- The accounts asked about, and the audit check's actor. Never the address or password_hash.
GRANT SELECT (id, role, disabled_at) ON TABLE public.users TO ownvitals_admin_connections;
-- The check that this transaction holds the matching ok audit row; never the IP's HMAC, never a
-- write.
GRANT SELECT (xmin, created_at, actor_id, action, target_user_id, outcome)
  ON TABLE public.admin_audit TO ownvitals_admin_connections;

-- Any other function under these names (an overload, an earlier draft) goes: each exists once.
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT p.oid::regprocedure::text AS signature
             FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public'
              AND p.proname IN ('admin_withings_states', 'admin_withings_counts')
              AND (p.oid::regprocedure::text NOT IN ('admin_withings_states(uuid[])',
                                                    'admin_withings_counts()')
                   OR pg_catalog.pg_get_function_result(p.oid) NOT IN (
                     'TABLE(user_id uuid, state text)', 'TABLE(state text, accounts bigint)'))
  LOOP
    EXECUTE format('DROP ROUTINE public.%s', r.signature);
  END LOOP;
END $$;

-- The Withings state of each account asked about (at most 101, the Users page plus one), in id
-- order: an id that names no account is left out.
CREATE OR REPLACE FUNCTION public.admin_withings_states(p_ids uuid[])
  RETURNS TABLE(user_id uuid, state text)
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
#variable_conflict use_column
BEGIN
  IF coalesce(pg_catalog.current_setting('app.user_id', true), '') <> '' THEN
    RAISE EXCEPTION 'bad_context' USING ERRCODE = '42501';
  END IF;
  IF NOT EXISTS (
       SELECT FROM public.admin_audit a JOIN public.users u ON u.id = a.actor_id
        WHERE a.xmin = pg_catalog.pg_current_xact_id()::xid
          AND a.created_at = pg_catalog.now() AND a.outcome = 'ok'
          AND ((a.action = 'user.list' AND a.target_user_id IS NULL)
               OR (a.action = 'user.read' AND pg_catalog.cardinality(p_ids) = 1
                   AND a.target_user_id = p_ids[1]))
          AND u.role = 'admin' AND u.disabled_at IS NULL) THEN
    RAISE EXCEPTION 'no_audit_row' USING ERRCODE = '42501';
  END IF;
  IF p_ids IS NULL OR pg_catalog.cardinality(p_ids) < 1 OR pg_catalog.cardinality(p_ids) > 101
     OR pg_catalog.array_position(p_ids, NULL) IS NOT NULL THEN
    RAISE EXCEPTION 'bad_argument' USING ERRCODE = '22023';
  END IF;
  RETURN QUERY
    SELECT u.id,
           CASE c.status WHEN 'connected' THEN 'healthy'
                         WHEN 'needs_reconnect' THEN 'needs_reconnect'
                         ELSE 'not_connected' END
      FROM public.users u
      LEFT JOIN public.connections c
        ON c.owner_id = u.id AND c.provider = 'withings' AND c.status <> 'disconnected'
     WHERE u.id = ANY (p_ids)
     ORDER BY u.id;
END
$fn$;

-- How many accounts are in each of the three states: always three rows, in name order.
CREATE OR REPLACE FUNCTION public.admin_withings_counts()
  RETURNS TABLE(state text, accounts bigint)
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
#variable_conflict use_column
BEGIN
  IF coalesce(pg_catalog.current_setting('app.user_id', true), '') <> '' THEN
    RAISE EXCEPTION 'bad_context' USING ERRCODE = '42501';
  END IF;
  IF NOT EXISTS (
       SELECT FROM public.admin_audit a JOIN public.users u ON u.id = a.actor_id
        WHERE a.xmin = pg_catalog.pg_current_xact_id()::xid
          AND a.created_at = pg_catalog.now() AND a.outcome = 'ok'
          AND a.action = 'overview.read' AND a.target_user_id IS NULL
          AND u.role = 'admin' AND u.disabled_at IS NULL) THEN
    RAISE EXCEPTION 'no_audit_row' USING ERRCODE = '42501';
  END IF;
  RETURN QUERY
    SELECT w.state, count(x.id)
      FROM (VALUES ('healthy'), ('needs_reconnect'), ('not_connected')) AS w(state)
      LEFT JOIN (
        SELECT u.id,
               CASE c.status WHEN 'connected' THEN 'healthy'
                             WHEN 'needs_reconnect' THEN 'needs_reconnect'
                             ELSE 'not_connected' END AS word
          FROM public.users u
          LEFT JOIN public.connections c
            ON c.owner_id = u.id AND c.provider = 'withings' AND c.status <> 'disconnected'
      ) AS x ON x.word = w.state
     GROUP BY w.state
     ORDER BY w.state;
END
$fn$;

ALTER FUNCTION public.admin_withings_states(uuid[]) OWNER TO ownvitals_admin_connections;
ALTER FUNCTION public.admin_withings_counts() OWNER TO ownvitals_admin_connections;

-- Every EXECUTE but the owner's goes (PUBLIC's default, and any grant to another role), then
-- the admin role gets it again.
REVOKE ALL ON FUNCTION public.admin_withings_states(uuid[]) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.admin_withings_counts() FROM PUBLIC;
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT DISTINCT p.oid::regprocedure::text AS signature,
                  a.grantee::regrole::text AS grantee, a.grantor::regrole::text AS grantor
             FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace,
                  aclexplode(p.proacl) a
            WHERE n.nspname = 'public'
              AND p.proname IN ('admin_withings_states', 'admin_withings_counts')
              AND a.grantee <> 0 AND a.grantee <> p.proowner LOOP
    EXECUTE format('SET LOCAL ROLE %s', r.grantor);
    EXECUTE format('REVOKE ALL ON FUNCTION public.%s FROM %s CASCADE', r.signature, r.grantee);
    EXECUTE 'RESET ROLE';
  END LOOP;
END $$;

GRANT EXECUTE ON FUNCTION public.admin_withings_states(uuid[]) TO ownvitals_maint;
GRANT EXECUTE ON FUNCTION public.admin_withings_counts() TO ownvitals_maint;

COMMENT ON FUNCTION public.admin_withings_states(uuid[]) IS 'ownvitals-admin-accounts v1';
COMMENT ON FUNCTION public.admin_withings_counts() IS 'ownvitals-admin-accounts v1';

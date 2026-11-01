-- The admin role ownvitals_maint and its column allowlist (ADR 0002 sections 4 and 5; #12;
-- classbeat's docker/postgres/maint/30-maint.sql at d806051).
--
-- Run as the superuser, after the migrations and 20-queue.sql, in one transaction. Plain SQL (no
-- psql meta-commands and no transaction control), so ownvitals.db.install_functions and the
-- tests run it the same way. The role's password and the stack wiring (compose, db-functions,
-- the Dockerfile) are #126: this script never sets or reads a password, and it creates the role
-- without one when it does not exist yet.
--
-- Idempotent: running it again repairs the role's attributes, memberships and settings, revokes
-- every privilege ownvitals_maint holds on anything in the database (table, column, sequence,
-- function, schema, database) and grants exactly the list below. Guard 11
-- (backend/tests/test_maint_privileges.py) checks the result as an allowlist, with a registry of
-- secret columns and health tables maint may never read; a change here needs the same change
-- there.
--
-- What maint may do: SELECT only on the columns the admin area shows, never a secret-bearing one
-- and never a column of a health-data table (ADR 0002 section 4: admins cannot read health
-- data); INSERT into the audit log; INSERT and UPDATE of the stored flags. Every transaction
-- starts read only (the role default) and ownvitals.admin.db audits each one.

DO $$
BEGIN
  IF NOT (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user) THEN
    RAISE EXCEPTION 'this script must run as the superuser';
  END IF;
  IF to_regclass('public.admin_audit') IS NULL OR to_regclass('public.app_settings') IS NULL THEN
    RAISE EXCEPTION 'migrate to head first: public.admin_audit or public.app_settings is missing';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_maint') THEN
    CREATE ROLE ownvitals_maint LOGIN;
  END IF;
END $$;

ALTER ROLE ownvitals_maint WITH LOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE
  NOREPLICATION NOINHERIT CONNECTION LIMIT -1;

-- A member of no role, and no role is a member of it (either would widen what it can do).
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT g.rolname AS granted, m.rolname AS member, x.rolname AS grantor
             FROM pg_catalog.pg_auth_members am
             JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
             JOIN pg_catalog.pg_roles m ON m.oid = am.member
             JOIN pg_catalog.pg_roles x ON x.oid = am.grantor
            WHERE 'ownvitals_maint' IN (g.rolname, m.rolname) LOOP
    EXECUTE format('REVOKE %I FROM %I GRANTED BY %I', r.granted, r.member, r.grantor);
  END LOOP;
END $$;

-- Role defaults: the app role's three timeouts, transaction_timeout (ADR 0002 section 5, #78)
-- and every transaction read only unless ownvitals.admin.db switches it (SET TRANSACTION READ
-- WRITE, then the audit row). Everything else set for the role goes first.
ALTER ROLE ownvitals_maint RESET ALL;
DO $$
BEGIN
  EXECUTE format('ALTER ROLE ownvitals_maint IN DATABASE %I RESET ALL', current_database());
END $$;
ALTER ROLE ownvitals_maint SET statement_timeout = '30s';
ALTER ROLE ownvitals_maint SET lock_timeout = '5s';
ALTER ROLE ownvitals_maint SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE ownvitals_maint SET transaction_timeout = '5min';
ALTER ROLE ownvitals_maint SET default_transaction_read_only = on;

-- Everything maint holds in this database goes first: table, column, sequence and function
-- privileges in every schema, then the schema and database privileges, and default privileges.
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT n.nspname, c.relname, c.relkind
             FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace,
                  aclexplode(c.relacl) a
            WHERE a.grantee = 'ownvitals_maint'::regrole GROUP BY 1, 2, 3 LOOP
    EXECUTE format('REVOKE ALL ON %s %I.%I FROM ownvitals_maint',
                   CASE WHEN r.relkind = 'S' THEN 'SEQUENCE' ELSE 'TABLE' END, r.nspname, r.relname);
  END LOOP;
  FOR r IN SELECT n.nspname, c.relname, a.attname
             FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
             JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace, aclexplode(a.attacl) x
            WHERE x.grantee = 'ownvitals_maint'::regrole GROUP BY 1, 2, 3 LOOP
    EXECUTE format('REVOKE ALL (%I) ON TABLE %I.%I FROM ownvitals_maint',
                   r.attname, r.nspname, r.relname);
  END LOOP;
  FOR r IN SELECT p.oid::regprocedure::text AS signature
             FROM pg_catalog.pg_proc p, aclexplode(p.proacl) a
            WHERE a.grantee = 'ownvitals_maint'::regrole LOOP
    EXECUTE format('REVOKE ALL ON ROUTINE %s FROM ownvitals_maint', r.signature);
  END LOOP;
  FOR r IN SELECT n.nspname FROM pg_catalog.pg_namespace n, aclexplode(n.nspacl) a
            WHERE a.grantee = 'ownvitals_maint'::regrole LOOP
    EXECUTE format('REVOKE ALL ON SCHEMA %I FROM ownvitals_maint', r.nspname);
  END LOOP;
  EXECUTE format('REVOKE ALL ON DATABASE %I FROM ownvitals_maint', current_database());
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO ownvitals_maint', current_database());
END $$;

GRANT USAGE ON SCHEMA public TO ownvitals_maint;

-- The audit log: insert and read (never the IP's HMAC); no UPDATE, DELETE or TRUNCATE, so it is
-- append-only for every runtime role (the app role holds nothing on it, migration 0006).
GRANT SELECT (id, created_at, actor_id, actor_email, action, target_user_id, target_kind,
              target_id, params, outcome, reason, request_id)
  ON public.admin_audit TO ownvitals_maint;
GRANT INSERT ON public.admin_audit TO ownvitals_maint;
GRANT USAGE ON SEQUENCE public.admin_audit_id_seq TO ownvitals_maint;

-- The stored flags: read, set and clear (clearing stores the JSON null: no DELETE).
GRANT SELECT (flag, value, created_at, updated_at, updated_by) ON public.app_settings
  TO ownvitals_maint;
GRANT INSERT (flag, value, updated_by) ON public.app_settings TO ownvitals_maint;
GRANT UPDATE (value, updated_at, updated_by) ON public.app_settings TO ownvitals_maint;

-- Jobs: counts per state and failures by kind and category; never the owner, input, keys or
-- hashes, and nothing of job_events.
GRANT SELECT (id, kind, state, error_category, created_at, finished_at)
  ON public.jobs TO ownvitals_maint;

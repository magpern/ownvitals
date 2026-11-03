-- Database roles, grants and defaults of ADR 0002 section 5.
--
-- Run as the superuser, against the application database, by the postgres image's init
-- (/docker-entrypoint-initdb.d, on the first start of an empty volume) or by hand / in CI:
--
--   psql -v ON_ERROR_STOP=1 -h HOST -U postgres -d ownvitals -f 10-roles.sql
--
-- Passwords are read from secret files, never from arguments or the environment:
--   $DATABASE_OWNER_PASSWORD_FILE  (default /run/secrets/database_owner_password)
--   $DATABASE_PASSWORD_FILE        (default /run/secrets/database_password)
--   $DATABASE_MAINT_PASSWORD_FILE  (default /run/secrets/database_maint_password)
-- The superuser's own password (postgres_password) is the image's POSTGRES_PASSWORD_FILE.
--
-- Idempotent: running it again repairs drift in what it manages (attributes, passwords,
-- memberships, role settings, grants and default privileges of the owner and app roles, and
-- the maint role's login and password). ACL kinds it does not manage (large objects, types,
-- parameters) are not touched. The function owner ownvitals_queue (ADR 0005 section 3, #9)
-- comes with its own script and is not touched here. ownvitals_maint's attributes,
-- memberships and grants are docker/postgres/maint/30-maint.sql (#12); this script only
-- creates that LOGIN role and sets its password from database_maint_password (#126).

\set ON_ERROR_STOP on
-- Same bytes as config.py: strip exactly one trailing LF, then one trailing CR, nothing else.
-- psql's backtick drops exactly one trailing LF of the command output (a shell `$(...)` would
-- drop them all); the CR is dropped below. The shell only expands the file name; the secret is
-- never interpreted by it, and reaches SQL only as a psql-quoted literal (:'name').
\set owner_raw `cat "${DATABASE_OWNER_PASSWORD_FILE:-/run/secrets/database_owner_password}"`
\set app_raw `cat "${DATABASE_PASSWORD_FILE:-/run/secrets/database_password}"`
\set maint_raw `cat "${DATABASE_MAINT_PASSWORD_FILE:-/run/secrets/database_maint_password}"`
SELECT CASE WHEN right(:'owner_raw', 1) = E'\r' THEN left(:'owner_raw', -1) ELSE :'owner_raw' END
         AS owner_password,
       CASE WHEN right(:'app_raw', 1) = E'\r' THEN left(:'app_raw', -1) ELSE :'app_raw' END
         AS app_password,
       CASE WHEN right(:'maint_raw', 1) = E'\r' THEN left(:'maint_raw', -1) ELSE :'maint_raw' END
         AS maint_password \gset
\unset owner_raw
\unset app_raw
\unset maint_raw

SELECT (:'owner_password' = '' OR :'app_password' = '' OR :'maint_password' = '')
         AS empty_password,
       NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS not_superuser \gset
\if :empty_password
  DO $$ BEGIN RAISE EXCEPTION 'a database password secret file is missing or empty'; END $$;
\endif
\if :not_superuser
  DO $$ BEGIN RAISE EXCEPTION 'this script must run as the superuser'; END $$;
\endif

-- Roles. The migration role owns everything; the app role owns nothing.
SELECT 'CREATE ROLE ownvitals_owner LOGIN'
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ownvitals_owner') \gexec
SELECT 'CREATE ROLE ownvitals_app LOGIN'
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ownvitals_app') \gexec
SELECT 'CREATE ROLE ownvitals_maint LOGIN'
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ownvitals_maint') \gexec

ALTER ROLE ownvitals_owner WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
  NOREPLICATION INHERIT CONNECTION LIMIT -1 PASSWORD :'owner_password';
ALTER ROLE ownvitals_app WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
  NOREPLICATION NOINHERIT CONNECTION LIMIT -1 PASSWORD :'app_password';
-- Login and password only. 30-maint.sql sets the attributes, settings and grants, and does
-- not change this password.
ALTER ROLE ownvitals_maint WITH LOGIN PASSWORD :'maint_password';

-- Neither the owner nor the app role is a member of any role (a BYPASSRLS membership would
-- defeat RLS through SET ROLE). ownvitals_maint's memberships are 30-maint.sql.
SELECT format('REVOKE %I FROM %I GRANTED BY %I', r.rolname, m.rolname, g.rolname)
  FROM pg_auth_members am
  JOIN pg_roles r ON r.oid = am.roleid
  JOIN pg_roles m ON m.oid = am.member
  JOIN pg_roles g ON g.oid = am.grantor
 WHERE m.rolname IN ('ownvitals_owner', 'ownvitals_app') \gexec

-- Role defaults, so every connection of the app role has them. They limit accidents (a stuck
-- lock, a runaway query, a transaction that stays busy). Injected SQL could still change them,
-- which web and worker detect at startup and on every new pooled connection (ADR 0002 section 5).
-- transaction_timeout is at most 5 minutes (ADR 0002 section 5, #78); the other three stay exact.
-- Everything else set for the role (also per database) is dropped first, so a re-run repairs it.
ALTER ROLE ownvitals_app RESET ALL;
SELECT format('ALTER ROLE ownvitals_app IN DATABASE %I RESET ALL', current_database()) \gexec
ALTER ROLE ownvitals_app SET statement_timeout = '30s';
ALTER ROLE ownvitals_app SET lock_timeout = '5s';
ALTER ROLE ownvitals_app SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE ownvitals_app SET transaction_timeout = '5min';

-- Every database of the server: PUBLIC cannot connect to it (both roles get CONNECT on this one
-- only, below).
SELECT format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', datname)
  FROM pg_database WHERE datallowconn \gexec

-- The database. PUBLIC loses CONNECT and TEMP; both roles get CONNECT explicitly.
SELECT format('ALTER DATABASE %I OWNER TO ownvitals_owner', current_database()) \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM ownvitals_app', current_database()) \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO ownvitals_owner, ownvitals_app',
              current_database()) \gexec

-- Schema public. PUBLIC loses USAGE and CREATE; the app role gets USAGE only.
ALTER SCHEMA public OWNER TO ownvitals_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM ownvitals_app;
GRANT USAGE, CREATE ON SCHEMA public TO ownvitals_owner;
GRANT USAGE ON SCHEMA public TO ownvitals_app;

-- What the owner creates from now on. No TRUNCATE (it ignores RLS), REFERENCES, TRIGGER or
-- MAINTAIN, and new functions are not callable by everyone (a SECURITY DEFINER function is
-- granted by name).
ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner IN SCHEMA public
  REVOKE ALL ON TABLES FROM ownvitals_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ownvitals_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner IN SCHEMA public
  REVOKE ALL ON SEQUENCES FROM ownvitals_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO ownvitals_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
-- Global defaults (no IN SCHEMA) granted to the app role or PUBLIC are dropped too. PUBLIC keeps
-- its built-in USAGE on types, which the app needs for the owner's enum types.
ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner REVOKE ALL ON TABLES FROM ownvitals_app, PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner REVOKE ALL ON SEQUENCES FROM ownvitals_app, PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner REVOKE ALL ON FUNCTIONS FROM ownvitals_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner REVOKE ALL ON TYPES FROM ownvitals_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ownvitals_owner REVOKE ALL ON SCHEMAS FROM ownvitals_app, PUBLIC;

-- The same for objects that already exist (this script may run again later).
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC, ownvitals_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ownvitals_app;
-- Tables whose migration grants the app role less than full DML are restored here, after the
-- blanket re-grant above (a table-level REVOKE also drops column grants); each later issue adds
-- its own lines with its migration. tests/test_roles_rerun.py checks a re-run leaves every table
-- as a plain migration does.
-- Migration bookkeeping stays closed to the app (migration 0002 revokes it too).
SELECT 'REVOKE ALL ON TABLE public.alembic_version FROM ownvitals_app, PUBLIC'
 WHERE to_regclass('public.alembic_version') IS NOT NULL \gexec
-- The key ring's check table (migration 0004, ADR 0004 section 3): the app role records and
-- reads check values, never changes or forgets one.
SELECT 'REVOKE UPDATE, DELETE ON TABLE public.connection_keks FROM ownvitals_app'
 WHERE to_regclass('public.connection_keks') IS NOT NULL \gexec
-- The stored flags and the admin audit log (migration 0006, #12): the app role reads the flags
-- and holds nothing on the audit log or its sequence.
SELECT 'REVOKE INSERT, UPDATE, DELETE ON TABLE public.app_settings FROM ownvitals_app'
 WHERE to_regclass('public.app_settings') IS NOT NULL \gexec
SELECT 'REVOKE ALL ON TABLE public.admin_audit FROM ownvitals_app'
 WHERE to_regclass('public.admin_audit') IS NOT NULL \gexec
-- withings_apps (#21): column SELECT only. The table REVOKE drops column grants, so the column
-- grant follows it. webhook_events is closed to the app role. Partitions are too.
SELECT 'REVOKE ALL ON TABLE public.withings_apps FROM ownvitals_app'
 WHERE to_regclass('public.withings_apps') IS NOT NULL \gexec
SELECT 'GRANT SELECT (id, client_id, callback_base, client_secret, client_secret_nonce, client_wrapped_dek, client_dek_nonce, client_key_id, webhook_secrets, rotation_requested_at, rotation_reason) ON TABLE public.withings_apps TO ownvitals_app'
 WHERE to_regclass('public.withings_apps') IS NOT NULL \gexec
SELECT 'REVOKE ALL ON TABLE public.webhook_events FROM ownvitals_app'
 WHERE to_regclass('public.webhook_events') IS NOT NULL \gexec
-- The rate tables (#23, migration 0010): no privilege for the app role.
SELECT 'REVOKE ALL ON TABLE public.withings_rate_buckets FROM ownvitals_app'
 WHERE to_regclass('public.withings_rate_buckets') IS NOT NULL \gexec
SELECT 'REVOKE ALL ON TABLE public.withings_rate_slots FROM ownvitals_app'
 WHERE to_regclass('public.withings_rate_slots') IS NOT NULL \gexec
SELECT 'REVOKE ALL ON TABLE public.withings_rate_demand FROM ownvitals_app'
 WHERE to_regclass('public.withings_rate_demand') IS NOT NULL \gexec
-- The AI cost cap (#205, migration 0013, ADR 0014 section 7): the app role reads its own budget
-- rows and writes neither; no privilege on the global row. Every write is a 50-ai.sql function.
SELECT 'REVOKE INSERT, UPDATE, DELETE ON TABLE public.ai_spend FROM ownvitals_app'
 WHERE to_regclass('public.ai_spend') IS NOT NULL \gexec
SELECT 'REVOKE INSERT, UPDATE, DELETE ON TABLE public.ai_reservations FROM ownvitals_app'
 WHERE to_regclass('public.ai_reservations') IS NOT NULL \gexec
SELECT 'REVOKE ALL ON TABLE public.ai_spend_global FROM ownvitals_app'
 WHERE to_regclass('public.ai_spend_global') IS NOT NULL \gexec
SELECT format('REVOKE ALL ON TABLE %s FROM ownvitals_app, PUBLIC', c.oid::regclass)
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relispartition \gexec
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, ownvitals_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ownvitals_app;
-- The audit log's id sequence (migration 0006, #12): nothing for the app role.
SELECT 'REVOKE ALL ON SEQUENCE public.admin_audit_id_seq FROM ownvitals_app'
 WHERE to_regclass('public.admin_audit_id_seq') IS NOT NULL \gexec
SELECT 'REVOKE ALL ON SEQUENCE public.withings_apps_id_seq FROM ownvitals_app'
 WHERE to_regclass('public.withings_apps_id_seq') IS NOT NULL \gexec
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;

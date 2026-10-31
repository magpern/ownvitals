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
-- The superuser's own password (postgres_password) is the image's POSTGRES_PASSWORD_FILE.
--
-- Idempotent: running it again repairs drift in what it manages (attributes, passwords,
-- memberships, role settings, grants and default privileges of the two roles). ACL kinds it
-- does not manage (large objects, types, parameters) are not touched. The function owners of
-- ADR 0005 section 3 (ownvitals_queue, #9) and the admin role ownvitals_maint (#12) come with
-- their own scripts; neither is touched here.

\set ON_ERROR_STOP on
-- Same bytes as config.py: strip exactly one trailing LF, then one trailing CR, nothing else.
-- psql's backtick drops exactly one trailing LF of the command output (a shell `$(...)` would
-- drop them all); the CR is dropped below. The shell only expands the file name; the secret is
-- never interpreted by it, and reaches SQL only as a psql-quoted literal (:'name').
\set owner_raw `cat "${DATABASE_OWNER_PASSWORD_FILE:-/run/secrets/database_owner_password}"`
\set app_raw `cat "${DATABASE_PASSWORD_FILE:-/run/secrets/database_password}"`
SELECT CASE WHEN right(:'owner_raw', 1) = E'\r' THEN left(:'owner_raw', -1) ELSE :'owner_raw' END
         AS owner_password,
       CASE WHEN right(:'app_raw', 1) = E'\r' THEN left(:'app_raw', -1) ELSE :'app_raw' END
         AS app_password \gset
\unset owner_raw
\unset app_raw

SELECT (:'owner_password' = '' OR :'app_password' = '') AS empty_password,
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

ALTER ROLE ownvitals_owner WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
  NOREPLICATION INHERIT CONNECTION LIMIT -1 PASSWORD :'owner_password';
ALTER ROLE ownvitals_app WITH LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
  NOREPLICATION NOINHERIT CONNECTION LIMIT -1 PASSWORD :'app_password';

-- Neither role is a member of any role (a BYPASSRLS membership would defeat RLS through SET ROLE).
SELECT format('REVOKE %I FROM %I GRANTED BY %I', r.rolname, m.rolname, g.rolname)
  FROM pg_auth_members am
  JOIN pg_roles r ON r.oid = am.roleid
  JOIN pg_roles m ON m.oid = am.member
  JOIN pg_roles g ON g.oid = am.grantor
 WHERE m.rolname IN ('ownvitals_owner', 'ownvitals_app') \gexec

-- Role defaults, so every connection of the app role has them. They limit accidents (a stuck
-- lock, a runaway query); injected SQL could still lower them, which web and worker detect at
-- startup and on every new pooled connection (ADR 0002 section 5).
-- Everything else set for the role (also per database) is dropped first, so a re-run repairs it.
ALTER ROLE ownvitals_app RESET ALL;
SELECT format('ALTER ROLE ownvitals_app IN DATABASE %I RESET ALL', current_database()) \gexec
ALTER ROLE ownvitals_app SET statement_timeout = '30s';
ALTER ROLE ownvitals_app SET lock_timeout = '5s';
ALTER ROLE ownvitals_app SET idle_in_transaction_session_timeout = '60s';

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
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC, ownvitals_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ownvitals_app;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC;

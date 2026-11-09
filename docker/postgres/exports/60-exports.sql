-- The export's cross-user purge (ADR 0013 section 4, ADR 0002 sections 3 and 4, ADR 0005
-- section 3; #333): one SECURITY DEFINER function, exports_purge_expired(), owned by its own
-- NOLOGIN role ownvitals_exports, called by the worker's hourly purge tick.
--
-- Run as the superuser, after `alembic upgrade head` and after 20-queue.sql, in ONE transaction.
-- Plain SQL: no psql meta-commands and no transaction control. Migrations never create this
-- function (handing a function to another role needs the superuser).
--
-- Idempotent: running it again repairs the role (attributes, memberships, settings), revokes and
-- re-grants exactly its privileges, and replaces the function with its owner, ACL and version
-- comment.
--
-- exports_purge_expired() takes no argument and returns four counts, nothing else:
--   expired: ready exports past expires_at, now expired (one export_expired event each);
--   failed:  building exports whose job has ended without making them ready, now failed with
--            build_failed (one export_failed event each), as the owner's own tidy does;
--   chunks:  chunks of failed and expired exports, deleted;
--   deleted: failed and expired export rows older than 90 days, deleted (the record of ADR 0013
--            section 4; any chunk left goes with them by the foreign key's cascade).
-- At most 100 exports per step and 1000 chunks per call, oldest first, so one call is bounded;
-- the worker calls again while a call still did something. Rows another transaction holds (a
-- download that is starting or taking its last chunk) are skipped and come at the next call.
--
-- It is the only cross-user export path. The owner's own tidy (ownvitals.reports.export_store)
-- does the same for one user under row-level security; this does it for owners who never come
-- back. The audit rows it writes are the owner's (user_id = the export's owner_id) and hold only
-- the export id, its kind and the fixed reason, the details ownvitals.auth.exports' models allow.
--
-- The role has BYPASSRLS (exports and export_chunks are under forced row-level security) and only
-- the column privileges the function needs: the state, age and job columns of exports, the ids
-- of export_chunks, the state of jobs, and INSERT of user_id, kind and detail on auth_events;
-- DELETE on exports and export_chunks. No health table, no other account table. 20-queue.sql
-- admits the function by name; its body is pinned by digest (ownvitals/db/queue_digests.py) and
-- checked by web and the worker at startup.

DO $$
BEGIN
  IF NOT coalesce((SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user), false)
  THEN
    RAISE EXCEPTION 'this script must run as the superuser';
  END IF;
  IF pg_catalog.to_regclass('public.exports') IS NULL
     OR pg_catalog.to_regclass('public.export_chunks') IS NULL
     OR pg_catalog.to_regclass('public.auth_events') IS NULL
     OR pg_catalog.to_regclass('public.jobs') IS NULL THEN
    RAISE EXCEPTION 'migrate to head first: exports, export_chunks, auth_events or jobs is missing';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_app') THEN
    RAISE EXCEPTION 'ownvitals_app does not exist; run docker/postgres/init/10-roles.sql first';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_exports') THEN
    CREATE ROLE ownvitals_exports NOLOGIN;
  END IF;
END $$;

-- No login and no other power than BYPASSRLS for its own function; a member of no role and
-- nobody a member of it; no role settings.
ALTER ROLE ownvitals_exports WITH NOLOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE
  NOREPLICATION NOINHERIT CONNECTION LIMIT -1 PASSWORD NULL;
ALTER ROLE ownvitals_exports RESET ALL;

DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT d.datname FROM pg_catalog.pg_database d
             JOIN pg_catalog.pg_db_role_setting s ON s.setdatabase = d.oid
            WHERE s.setrole = 'ownvitals_exports'::regrole LOOP
    EXECUTE format('ALTER ROLE ownvitals_exports IN DATABASE %I RESET ALL', r.datname);
  END LOOP;
  FOR r IN SELECT g.rolname AS granted, m.rolname AS member, gr.rolname AS grantor
             FROM pg_catalog.pg_auth_members am
             JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
             JOIN pg_catalog.pg_roles m ON m.oid = am.member
             JOIN pg_catalog.pg_roles gr ON gr.oid = am.grantor
            WHERE 'ownvitals_exports' IN (g.rolname, m.rolname) LOOP
    EXECUTE format('REVOKE %I FROM %I GRANTED BY %I CASCADE', r.granted, r.member, r.grantor);
  END LOOP;
END $$;

-- ownvitals_exports owns exports_purge_expired and nothing else, and has no default privileges
-- of its own. Anything else is not repaired automatically (dropping or reassigning it could lose
-- data): the script stops and names it.
DO $$
DECLARE
  q oid := 'ownvitals_exports'::regrole;
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
                         AND p.proname = 'exports_purge_expired') THEN
      RAISE EXCEPTION 'ownvitals_exports owns % (catalog %, object %); it may own only the '
        'export purge function: drop or reassign it by hand',
        coalesce(r.what, '?'), r.catalog, r.objid;
    END IF;
  END LOOP;
  IF EXISTS (SELECT FROM pg_catalog.pg_default_acl WHERE defaclrole = q) THEN
    RAISE EXCEPTION 'default privileges exist FOR ROLE ownvitals_exports; remove them by hand';
  END IF;
END $$;

-- Revoke every privilege ownvitals_exports holds, found through pg_shdepend (every ACL entry naming
-- a role is recorded there, for every object kind). A kind this block does not know stops the
-- script: repair it by hand rather than run with an unknown privilege.
DO $$
DECLARE
  q oid := 'ownvitals_exports'::regrole;
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
        EXECUTE format('REVOKE ALL ON %s FROM ownvitals_exports CASCADE', target);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_class' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, t.attname
                 FROM pg_catalog.pg_attribute t, aclexplode(t.attacl) a
                WHERE t.attrelid = d.objid AND t.attnum = d.objsubid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL (%I) ON TABLE %s FROM ownvitals_exports CASCADE',
                       g.attname, d.objid::regclass::text);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_namespace' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, n.nspname
                 FROM pg_catalog.pg_namespace n, aclexplode(n.nspacl) a
                WHERE n.oid = d.objid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM ownvitals_exports CASCADE', g.nspname);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_proc' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor
                 FROM pg_catalog.pg_proc p, aclexplode(p.proacl) a
                WHERE p.oid = d.objid AND a.grantee = q AND p.proowner <> q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON ROUTINE %s FROM ownvitals_exports CASCADE',
                       d.objid::regprocedure::text);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_database' THEN
      FOR g IN SELECT DISTINCT a.grantor::regrole::text AS grantor, b.datname
                 FROM pg_catalog.pg_database b, aclexplode(b.datacl) a
                WHERE b.oid = d.objid AND a.grantee = q LOOP
        EXECUTE format('SET LOCAL ROLE %s', g.grantor);
        EXECUTE format('REVOKE ALL ON DATABASE %I FROM ownvitals_exports CASCADE', g.datname);
        EXECUTE 'RESET ROLE';
      END LOOP;
    ELSIF d.catalog = 'pg_default_acl' THEN
      FOR g IN SELECT x.defaclrole::regrole::text AS owner, x.defaclnamespace,
                      CASE x.defaclobjtype WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES'
                           WHEN 'f' THEN 'FUNCTIONS' WHEN 'T' THEN 'TYPES'
                           WHEN 'n' THEN 'SCHEMAS' END AS kind
                 FROM pg_catalog.pg_default_acl x WHERE x.oid = d.objid LOOP
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %s %s REVOKE ALL ON %s '
                       'FROM ownvitals_exports CASCADE', g.owner,
                       CASE WHEN g.defaclnamespace = 0 THEN ''
                            ELSE 'IN SCHEMA ' || quote_ident(g.defaclnamespace::regnamespace::text)
                       END, g.kind);
      END LOOP;
    ELSE
      RAISE EXCEPTION 'ownvitals_exports holds a privilege on % %; revoke it by hand',
        d.catalog, d.objid;
    END IF;
  END LOOP;
END $$;

-- Exactly these privileges: USAGE on public; on exports the columns the function reads and
-- returns, UPDATE of state and failure, and DELETE; on export_chunks the columns that join a
-- chunk to its export, and DELETE; on jobs whether the build's job has ended; on auth_events
-- INSERT of the owner, the kind and the detail (the other columns take their defaults or stay
-- NULL: no address, no origin). Everything else was revoked above.
GRANT USAGE ON SCHEMA public TO ownvitals_exports;

GRANT SELECT (id, owner_id, kind, state, job_id, created_at, expires_at)
  ON TABLE public.exports TO ownvitals_exports;
GRANT UPDATE (state, failure) ON TABLE public.exports TO ownvitals_exports;
GRANT DELETE ON TABLE public.exports TO ownvitals_exports;

GRANT SELECT (id, owner_id, export_id) ON TABLE public.export_chunks TO ownvitals_exports;
GRANT DELETE ON TABLE public.export_chunks TO ownvitals_exports;

GRANT SELECT (id, owner_id, state) ON TABLE public.jobs TO ownvitals_exports;

GRANT INSERT (user_id, kind, detail) ON TABLE public.auth_events TO ownvitals_exports;

-- Any other function under the name (an overload) goes: it must exist once.
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT p.oid::regprocedure::text AS signature
             FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'public' AND p.proname = 'exports_purge_expired'
              AND p.oid::regprocedure::text <> 'exports_purge_expired()' LOOP
    EXECUTE format('DROP ROUTINE public.%s', r.signature);
  END LOOP;
END $$;

-- The purge (ADR 0013 section 4). Every owner's exports; nothing but the export rows, their
-- chunks and the audit rows of what it changed. The event kinds and details are fixed here.
CREATE OR REPLACE FUNCTION public.exports_purge_expired(
  OUT expired integer, OUT failed integer, OUT chunks integer, OUT deleted integer)
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
BEGIN
  WITH gone AS (
    UPDATE public.exports e SET state = 'expired'
     WHERE e.id IN (SELECT x.id FROM public.exports x
                     WHERE x.state = 'ready' AND x.expires_at <= pg_catalog.now()
                     ORDER BY x.expires_at, x.id
                     LIMIT 100
                     FOR UPDATE SKIP LOCKED)
    RETURNING e.id, e.owner_id, e.kind)
  INSERT INTO public.auth_events (user_id, kind, detail)
  SELECT g.owner_id, 'export_expired',
         pg_catalog.jsonb_build_object('export_id', g.id, 'export_kind', g.kind)
    FROM gone g;
  GET DIAGNOSTICS expired = ROW_COUNT;

  WITH ended AS (
    UPDATE public.exports e SET state = 'failed', failure = 'build_failed'
     WHERE e.id IN (SELECT x.id FROM public.exports x
                      JOIN public.jobs j ON j.id = x.job_id AND j.owner_id = x.owner_id
                     WHERE x.state = 'building'
                       AND j.state IN ('completed', 'failed', 'dead_letter', 'cancelled')
                     ORDER BY x.created_at, x.id
                     LIMIT 100
                     FOR UPDATE OF x SKIP LOCKED)
    RETURNING e.id, e.owner_id, e.kind)
  INSERT INTO public.auth_events (user_id, kind, detail)
  SELECT d.owner_id, 'export_failed',
         pg_catalog.jsonb_build_object('export_id', d.id, 'export_kind', d.kind,
                                       'reason', 'build_failed')
    FROM ended d;
  GET DIAGNOSTICS failed = ROW_COUNT;

  DELETE FROM public.export_chunks c
   WHERE c.id IN (SELECT k.id FROM public.export_chunks k
                    JOIN public.exports x ON x.owner_id = k.owner_id AND x.id = k.export_id
                   WHERE x.state IN ('failed', 'expired')
                   LIMIT 1000);
  GET DIAGNOSTICS chunks = ROW_COUNT;

  DELETE FROM public.exports e
   WHERE e.id IN (SELECT x.id FROM public.exports x
                   WHERE x.state IN ('failed', 'expired')
                     AND x.created_at < pg_catalog.now() - pg_catalog.make_interval(days => 90)
                   ORDER BY x.created_at, x.id
                   LIMIT 100
                   FOR UPDATE SKIP LOCKED);
  GET DIAGNOSTICS deleted = ROW_COUNT;
END
$fn$;

ALTER FUNCTION public.exports_purge_expired() OWNER TO ownvitals_exports;

-- Every EXECUTE but the owner's goes (PUBLIC's default, and any grant to another role), then
-- the app role (the worker) gets it again.
REVOKE ALL ON FUNCTION public.exports_purge_expired() FROM PUBLIC, ownvitals_app;
DO $$
DECLARE
  r record;
BEGIN
  FOR r IN SELECT DISTINCT p.oid::regprocedure::text AS signature,
                  a.grantee::regrole::text AS grantee, a.grantor::regrole::text AS grantor
             FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace,
                  aclexplode(p.proacl) a
            WHERE n.nspname = 'public' AND p.proname = 'exports_purge_expired'
              AND a.grantee <> 0 AND a.grantee <> p.proowner LOOP
    EXECUTE format('SET LOCAL ROLE %s', r.grantor);
    EXECUTE format('REVOKE ALL ON FUNCTION public.%s FROM %s CASCADE', r.signature, r.grantee);
    EXECUTE 'RESET ROLE';
  END LOOP;
END $$;

-- The grantor is the function owner, whether this script runs as that role or as a superuser.
GRANT EXECUTE ON FUNCTION public.exports_purge_expired() TO ownvitals_app;

COMMENT ON FUNCTION public.exports_purge_expired() IS 'ownvitals-exports v1';

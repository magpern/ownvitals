-- Withings partition and webhook-secret functions (ADR 0002 sections 2 and 3, ADR 0004
-- section 4, ADR 0005 sections 3 and 9, ADR 0008; #21).
--
-- Run as the superuser, after `alembic upgrade head`, in ONE transaction. Plain SQL: no psql
-- meta-commands and no transaction control. Migrations never create these functions.
--
-- Idempotent: running it again repairs the two NOLOGIN roles, their grants, the functions and
-- the app-role column grants on withings_apps. ownvitals_webhook_secrets has no BYPASSRLS.
-- ownvitals_webhook_events inserts webhook_events and nothing else.
--
-- 20-queue.sql admits exactly ensure_month_partitions, webhook_secrets_apply and
-- webhook_event_insert, and no other function. A re-run of that script after this one keeps them.

DO $$
BEGIN
  IF NOT coalesce((SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user), false)
  THEN
    RAISE EXCEPTION 'this script must run as the superuser';
  END IF;
  IF to_regclass('public.withings_apps') IS NULL
     OR to_regclass('public.webhook_events') IS NULL
     OR to_regclass('public.sleep_series') IS NULL
     OR to_regclass('public.intraday') IS NULL
     OR to_regclass('public.admin_audit') IS NULL
     OR to_regclass('public.connection_keks') IS NULL
     OR to_regclass('public.admin_audit_id_seq') IS NULL THEN
    RAISE EXCEPTION 'migrate to head first';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_app') THEN
    RAISE EXCEPTION 'ownvitals_app does not exist; run docker/postgres/init/10-roles.sql first';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_maint') THEN
    CREATE ROLE ownvitals_maint LOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_webhook_secrets') THEN
    CREATE ROLE ownvitals_webhook_secrets NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_webhook_events') THEN
    CREATE ROLE ownvitals_webhook_events NOLOGIN;
  END IF;
END $$;

ALTER ROLE ownvitals_webhook_secrets WITH NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
  NOCREATEROLE NOREPLICATION NOINHERIT CONNECTION LIMIT -1 PASSWORD NULL;
ALTER ROLE ownvitals_webhook_events WITH NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB
  NOCREATEROLE NOREPLICATION NOINHERIT CONNECTION LIMIT -1 PASSWORD NULL;
ALTER ROLE ownvitals_webhook_secrets RESET ALL;
ALTER ROLE ownvitals_webhook_events RESET ALL;

DO $$
DECLARE
  v_role text;
  r record;
BEGIN
  FOREACH v_role IN ARRAY ARRAY['ownvitals_webhook_secrets', 'ownvitals_webhook_events'] LOOP
    FOR r IN SELECT g.rolname AS granted, m.rolname AS member, gr.rolname AS grantor
               FROM pg_catalog.pg_auth_members am
               JOIN pg_catalog.pg_roles g ON g.oid = am.roleid
               JOIN pg_catalog.pg_roles m ON m.oid = am.member
               JOIN pg_catalog.pg_roles gr ON gr.oid = am.grantor
              WHERE v_role IN (g.rolname, m.rolname) LOOP
      EXECUTE format('REVOKE %I FROM %I GRANTED BY %I CASCADE', r.granted, r.member, r.grantor);
    END LOOP;
  END LOOP;
END $$;

-- Exactly the column grants of ADR 0002 section 3, plus the ingest role's INSERT.
REVOKE ALL ON SCHEMA public FROM ownvitals_webhook_secrets, ownvitals_webhook_events;
GRANT USAGE ON SCHEMA public TO ownvitals_webhook_secrets, ownvitals_webhook_events;
REVOKE CREATE ON SCHEMA public FROM ownvitals_webhook_secrets, ownvitals_webhook_events;

REVOKE ALL ON TABLE public.withings_apps FROM ownvitals_webhook_secrets;
GRANT UPDATE (webhook_secrets, rotation_requested_at, rotation_reason)
  ON TABLE public.withings_apps TO ownvitals_webhook_secrets;
GRANT SELECT (id, webhook_secrets, rotation_requested_at, rotation_reason)
  ON TABLE public.withings_apps TO ownvitals_webhook_secrets;

REVOKE ALL ON TABLE public.connection_keks FROM ownvitals_webhook_secrets;
GRANT SELECT (key_id) ON TABLE public.connection_keks TO ownvitals_webhook_secrets;

REVOKE ALL ON TABLE public.admin_audit FROM ownvitals_webhook_secrets;
GRANT INSERT ON TABLE public.admin_audit TO ownvitals_webhook_secrets;
REVOKE ALL ON SEQUENCE public.admin_audit_id_seq FROM ownvitals_webhook_secrets;
GRANT USAGE ON SEQUENCE public.admin_audit_id_seq TO ownvitals_webhook_secrets;

REVOKE ALL ON TABLE public.webhook_events FROM ownvitals_webhook_events;
GRANT INSERT ON TABLE public.webhook_events TO ownvitals_webhook_events;

-- Repair the app and maint grants the migration and 30-maint.sql also set.
REVOKE ALL ON TABLE public.withings_apps FROM ownvitals_app;
GRANT SELECT (id, client_id, callback_base, client_secret, client_secret_nonce,
              client_wrapped_dek, client_dek_nonce, client_key_id, webhook_secrets,
              rotation_requested_at, rotation_reason)
  ON TABLE public.withings_apps TO ownvitals_app;
REVOKE ALL ON SEQUENCE public.withings_apps_id_seq FROM ownvitals_app;
REVOKE ALL ON TABLE public.webhook_events FROM ownvitals_app;

REVOKE ALL ON TABLE public.withings_apps FROM ownvitals_maint;
GRANT SELECT (id, client_id, callback_base, rotation_requested_at, rotation_reason)
  ON TABLE public.withings_apps TO ownvitals_maint;

CREATE OR REPLACE FUNCTION public.ensure_month_partitions(
    p_table text, p_from date, p_to date)
  RETURNS integer
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
  SET TimeZone TO 'UTC'
AS $fn$
DECLARE
  v_parent oid;
  v_month date;
  v_next date;
  v_name text;
  v_created integer := 0;
  v_ceiling timestamptz := pg_catalog.now() + interval '3 months';
BEGIN
  IF p_table NOT IN ('sleep_series', 'intraday') THEN
    RAISE EXCEPTION 'table_not_allowed' USING ERRCODE = '22023';
  END IF;
  IF p_from IS NULL OR p_to IS NULL
     OR p_from <> date_trunc('month', p_from)::date
     OR p_to <> date_trunc('month', p_to)::date THEN
    RAISE EXCEPTION 'month_not_aligned' USING ERRCODE = '22023';
  END IF;
  IF p_from > p_to THEN
    RAISE EXCEPTION 'empty_range' USING ERRCODE = '22023';
  END IF;
  v_month := p_from;
  WHILE v_month <= p_to LOOP
    IF v_month < DATE '2009-01-01'
       OR (v_month::timestamp AT TIME ZONE 'UTC') >= v_ceiling THEN
      RAISE EXCEPTION 'month_out_of_range' USING ERRCODE = '22023';
    END IF;
    v_month := (v_month + interval '1 month')::date;
  END LOOP;
  SELECT c.oid INTO v_parent
    FROM pg_catalog.pg_class c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = 'public' AND c.relname = p_table AND c.relkind = 'p';
  IF v_parent IS NULL THEN
    RAISE EXCEPTION 'table_missing' USING ERRCODE = '22023';
  END IF;
  -- Namespace 3, the parent's oid (ADR 0005 section 9). Held until the caller commits.
  PERFORM pg_catalog.pg_advisory_xact_lock(3, v_parent::integer);
  v_month := p_from;
  WHILE v_month <= p_to LOOP
    v_next := (v_month + interval '1 month')::date;
    v_name := p_table || '_y' || to_char(v_month, 'YYYY') || 'm' || to_char(v_month, 'MM');
    IF to_regclass(format('public.%I', v_name)) IS NULL THEN
      EXECUTE format(
        'CREATE TABLE public.%I PARTITION OF public.%I FOR VALUES FROM (%L) TO (%L)',
        v_name, p_table, v_month, v_next);
      v_created := v_created + 1;
    END IF;
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', v_name);
    EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', v_name);
    IF NOT EXISTS (
      SELECT 1 FROM pg_catalog.pg_policy p
      JOIN pg_catalog.pg_class c ON c.oid = p.polrelid
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'public' AND c.relname = v_name AND p.polname = 'owner_isolation'
    ) THEN
      EXECUTE format(
        'CREATE POLICY owner_isolation ON public.%I AS PERMISSIVE FOR ALL TO PUBLIC '
        'USING (owner_id = NULLIF(current_setting(''app.user_id'', true), '''')::uuid) '
        'WITH CHECK (owner_id = NULLIF(current_setting(''app.user_id'', true), '''')::uuid)',
        v_name);
    END IF;
    EXECUTE format('REVOKE ALL ON TABLE public.%I FROM ownvitals_app, PUBLIC', v_name);
    v_month := v_next;
  END LOOP;
  RETURN v_created;
END
$fn$;

CREATE OR REPLACE FUNCTION public.webhook_secrets_apply(
    p_app_id integer,
    p_operation text,
    p_secret_id text DEFAULT NULL,
    p_seed_id text DEFAULT NULL,
    p_pepper_id text DEFAULT NULL,
    p_hmac bytea DEFAULT NULL,
    p_active_from timestamptz DEFAULT NULL,
    p_seen_requested_at timestamptz DEFAULT NULL,
    p_seen_reason text DEFAULT NULL)
  RETURNS text
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  v_entries jsonb;
  v_requested_at timestamptz;
  v_reason text;
  v_new jsonb;
  v_acted jsonb := '[]'::jsonb;
  v_now timestamptz := pg_catalog.now();
  v_active integer;
  v_found integer;
BEGIN
  -- Isolation before the lock (ADR 0004 section 4). Any other level is refused.
  IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'bad_isolation' USING ERRCODE = '22023';
  END IF;
  IF p_app_id IS NULL THEN
    RAISE EXCEPTION 'unknown_app' USING ERRCODE = '22023';
  END IF;
  -- Namespace 4, the app id (ADR 0005 section 9). Then the row lock, then every other check.
  PERFORM pg_catalog.pg_advisory_xact_lock(4, p_app_id);
  SELECT a.webhook_secrets, a.rotation_requested_at, a.rotation_reason
    INTO v_entries, v_requested_at, v_reason
    FROM public.withings_apps a
   WHERE a.id = p_app_id
     FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'unknown_app' USING ERRCODE = '22023';
  END IF;
  IF p_operation NOT IN ('append', 'retire', 'retire-all', 'rehash', 'delete-retired') THEN
    RAISE EXCEPTION 'bad_operation' USING ERRCODE = '22023';
  END IF;
  IF p_operation IN ('append', 'retire-all') THEN
    IF v_requested_at IS DISTINCT FROM p_seen_requested_at THEN
      RAISE EXCEPTION 'stale_request' USING ERRCODE = '22023';
    END IF;
    IF v_reason = 'compromise' AND p_seen_reason IS DISTINCT FROM 'compromise' THEN
      RAISE EXCEPTION 'compromise_pending' USING ERRCODE = '22023';
    END IF;
  END IF;

  IF p_operation = 'append' THEN
    IF p_secret_id IS NULL
       OR p_secret_id !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
      RAISE EXCEPTION 'bad_secret_id' USING ERRCODE = '22023';
    END IF;
    IF p_seed_id IS NULL OR p_seed_id !~ '^seed-[a-z0-9-]{1,28}$'
       OR NOT EXISTS (SELECT 1 FROM public.connection_keks k WHERE k.key_id = p_seed_id) THEN
      RAISE EXCEPTION 'unknown_seed' USING ERRCODE = '22023';
    END IF;
    IF p_pepper_id IS NULL OR p_pepper_id !~ '^pep-[a-z0-9-]{1,28}$'
       OR NOT EXISTS (SELECT 1 FROM public.connection_keks k WHERE k.key_id = p_pepper_id) THEN
      RAISE EXCEPTION 'unknown_pepper' USING ERRCODE = '22023';
    END IF;
    IF p_hmac IS NULL OR pg_catalog.octet_length(p_hmac) <> 32 THEN
      RAISE EXCEPTION 'bad_hmac' USING ERRCODE = '22023';
    END IF;
    IF p_active_from IS NULL THEN
      RAISE EXCEPTION 'bad_active_from' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(v_entries) e
                WHERE e->>'secret_id' = p_secret_id) THEN
      RAISE EXCEPTION 'secret_exists' USING ERRCODE = '22023';
    END IF;
    SELECT count(*) INTO v_active FROM pg_catalog.jsonb_array_elements(v_entries) e
     WHERE e->>'retire_at' IS NULL;
    IF v_active >= 2 THEN
      RAISE EXCEPTION 'too_many_active' USING ERRCODE = '22023';
    END IF;
    -- A clearing append of a compromise request (ADR 0004 section 4, amended #72).
    IF v_reason = 'compromise' THEN
      IF v_active > 0 THEN
        RAISE EXCEPTION 'unretired_entries' USING ERRCODE = '22023';
      END IF;
      IF EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(v_entries) e
                  WHERE e->>'seed_id' = p_seed_id AND e->>'retire_at' IS NOT NULL) THEN
        RAISE EXCEPTION 'seed_reused' USING ERRCODE = '22023';
      END IF;
    END IF;
    v_new := v_entries || pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
      'secret_id', p_secret_id, 'seed_id', p_seed_id, 'pepper_id', p_pepper_id,
      'hmac', pg_catalog.encode(p_hmac, 'hex'), 'active_from', p_active_from, 'retire_at', NULL));
    v_acted := pg_catalog.jsonb_build_array(pg_catalog.jsonb_build_object(
      'secret_id', p_secret_id, 'seed_id', p_seed_id, 'pepper_id', p_pepper_id));
  ELSIF p_operation = 'retire' THEN
    SELECT count(*) INTO v_found FROM pg_catalog.jsonb_array_elements(v_entries) e
     WHERE e->>'secret_id' = p_secret_id;
    IF v_found <> 1 THEN
      RAISE EXCEPTION 'unknown_secret' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(v_entries) e
                WHERE e->>'secret_id' = p_secret_id AND e->>'retire_at' IS NOT NULL) THEN
      RAISE EXCEPTION 'already_retired' USING ERRCODE = '22023';
    END IF;
    SELECT coalesce(pg_catalog.jsonb_agg(
             CASE WHEN e->>'secret_id' = p_secret_id
                  THEN e || pg_catalog.jsonb_build_object('retire_at', v_now) ELSE e END),
           '[]'::jsonb)
      INTO v_new FROM pg_catalog.jsonb_array_elements(v_entries) e;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
             'secret_id', e->>'secret_id', 'seed_id', e->>'seed_id',
             'pepper_id', e->>'pepper_id')), '[]'::jsonb)
      INTO v_acted FROM pg_catalog.jsonb_array_elements(v_entries) e
     WHERE e->>'secret_id' = p_secret_id;
  ELSIF p_operation = 'retire-all' THEN
    SELECT coalesce(pg_catalog.jsonb_agg(
             CASE WHEN e->>'retire_at' IS NULL
                  THEN e || pg_catalog.jsonb_build_object('retire_at', v_now) ELSE e END),
           '[]'::jsonb)
      INTO v_new FROM pg_catalog.jsonb_array_elements(v_entries) e;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
             'secret_id', e->>'secret_id', 'seed_id', e->>'seed_id',
             'pepper_id', e->>'pepper_id')), '[]'::jsonb)
      INTO v_acted FROM pg_catalog.jsonb_array_elements(v_entries) e
     WHERE e->>'retire_at' IS NULL;
  ELSIF p_operation = 'rehash' THEN
    IF p_pepper_id IS NULL OR p_pepper_id !~ '^pep-[a-z0-9-]{1,28}$'
       OR NOT EXISTS (SELECT 1 FROM public.connection_keks k WHERE k.key_id = p_pepper_id) THEN
      RAISE EXCEPTION 'unknown_pepper' USING ERRCODE = '22023';
    END IF;
    IF p_hmac IS NULL OR pg_catalog.octet_length(p_hmac) <> 32 THEN
      RAISE EXCEPTION 'bad_hmac' USING ERRCODE = '22023';
    END IF;
    SELECT count(*) INTO v_found FROM pg_catalog.jsonb_array_elements(v_entries) e
     WHERE e->>'secret_id' = p_secret_id;
    IF v_found <> 1 THEN
      RAISE EXCEPTION 'unknown_secret' USING ERRCODE = '22023';
    END IF;
    SELECT coalesce(pg_catalog.jsonb_agg(
             CASE WHEN e->>'secret_id' = p_secret_id
                  THEN e || pg_catalog.jsonb_build_object(
                         'pepper_id', p_pepper_id, 'hmac', pg_catalog.encode(p_hmac, 'hex'))
                  ELSE e END), '[]'::jsonb)
      INTO v_new FROM pg_catalog.jsonb_array_elements(v_entries) e;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
             'secret_id', e->>'secret_id', 'seed_id', e->>'seed_id', 'pepper_id', p_pepper_id)),
           '[]'::jsonb)
      INTO v_acted FROM pg_catalog.jsonb_array_elements(v_entries) e
     WHERE e->>'secret_id' = p_secret_id;
  ELSE
    SELECT count(*) INTO v_found FROM pg_catalog.jsonb_array_elements(v_entries) e
     WHERE e->>'secret_id' = p_secret_id AND e->>'retire_at' IS NOT NULL;
    IF v_found <> 1 THEN
      IF EXISTS (SELECT 1 FROM pg_catalog.jsonb_array_elements(v_entries) e
                  WHERE e->>'secret_id' = p_secret_id) THEN
        RAISE EXCEPTION 'not_retired' USING ERRCODE = '22023';
      END IF;
      RAISE EXCEPTION 'unknown_secret' USING ERRCODE = '22023';
    END IF;
    SELECT coalesce(pg_catalog.jsonb_agg(e), '[]'::jsonb) INTO v_new
      FROM pg_catalog.jsonb_array_elements(v_entries) e
     WHERE e->>'secret_id' IS DISTINCT FROM p_secret_id;
    SELECT coalesce(pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
             'secret_id', e->>'secret_id', 'seed_id', e->>'seed_id',
             'pepper_id', e->>'pepper_id')), '[]'::jsonb)
      INTO v_acted FROM pg_catalog.jsonb_array_elements(v_entries) e
     WHERE e->>'secret_id' = p_secret_id;
  END IF;

  UPDATE public.withings_apps SET
    webhook_secrets = v_new,
    rotation_requested_at = CASE WHEN p_operation = 'append' THEN NULL
                                 ELSE rotation_requested_at END,
    rotation_reason = CASE WHEN p_operation = 'append' THEN NULL ELSE rotation_reason END
   WHERE id = p_app_id;
  -- Ids only. The hmac stays in webhook_secrets and never reaches this row.
  INSERT INTO public.admin_audit (
      actor_email, action, target_kind, target_id, params, outcome, request_id, ip_hash)
  VALUES (
      'ownvitals-webhook-secrets@localhost', 'webhook_secrets.apply', 'withings_app',
      p_app_id::text,
      pg_catalog.jsonb_build_object('operation', p_operation, 'entries', v_acted),
      'ok', 'webhook-secrets',
      pg_catalog.decode(repeat('00', 32), 'hex'));
  RETURN p_operation;
END
$fn$;

CREATE OR REPLACE FUNCTION public.webhook_event_insert(
    p_app_id integer,
    p_withings_userid bigint,
    p_appli integer,
    p_startdate bigint,
    p_enddate bigint,
    p_dedupe_key text,
    p_job_id uuid)
  RETURNS uuid
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
  v_id uuid;
BEGIN
  IF p_dedupe_key IS NULL OR p_dedupe_key !~ '^[A-Za-z0-9._:-]{1,200}$' THEN
    RAISE EXCEPTION 'bad_dedupe_key' USING ERRCODE = '22023';
  END IF;
  v_id := pg_catalog.gen_random_uuid();
  INSERT INTO public.webhook_events (
      id, app_id, withings_userid, appli, startdate, enddate, dedupe_key, job_id)
  VALUES (
      v_id, p_app_id, p_withings_userid, p_appli, p_startdate, p_enddate, p_dedupe_key, p_job_id);
  RETURN v_id;
EXCEPTION
  WHEN unique_violation THEN
    RAISE EXCEPTION 'duplicate' USING ERRCODE = '22023';
END
$fn$;

ALTER FUNCTION public.ensure_month_partitions(text, date, date) OWNER TO ownvitals_owner;
ALTER FUNCTION public.webhook_secrets_apply(
    integer, text, text, text, text, bytea, timestamptz, timestamptz, text)
  OWNER TO ownvitals_webhook_secrets;
ALTER FUNCTION public.webhook_event_insert(integer, bigint, integer, bigint, bigint, text, uuid)
  OWNER TO ownvitals_webhook_events;

REVOKE ALL ON FUNCTION public.ensure_month_partitions(text, date, date) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.webhook_secrets_apply(
    integer, text, text, text, text, bytea, timestamptz, timestamptz, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.webhook_event_insert(integer, bigint, integer, bigint, bigint, text, uuid)
  FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.ensure_month_partitions(text, date, date) TO ownvitals_app;
GRANT EXECUTE ON FUNCTION public.webhook_secrets_apply(
    integer, text, text, text, text, bytea, timestamptz, timestamptz, text) TO ownvitals_app;
GRANT EXECUTE ON FUNCTION public.webhook_event_insert(integer, bigint, integer, bigint, bigint, text, uuid)
  TO ownvitals_app;

COMMENT ON FUNCTION public.ensure_month_partitions(text, date, date) IS 'ownvitals-withings v1';
COMMENT ON FUNCTION public.webhook_secrets_apply(
    integer, text, text, text, text, bytea, timestamptz, timestamptz, text)
  IS 'ownvitals-withings v1';
COMMENT ON FUNCTION public.webhook_event_insert(integer, bigint, integer, bigint, bigint, text, uuid)
  IS 'ownvitals-withings v1';

-- Withings rate limiter (PLAN section 4.4, ADR 0005 section 8; #23).
--
-- Run as the superuser or as ownvitals_owner, after the rate tables exist, in ONE transaction.
-- Plain SQL: no psql meta-commands and no transaction control. Idempotent.
--
-- One bucket per Withings app, budget at most 100 a minute. A token is consumed when a claim
-- is granted and is never refunded. Four global slots (the in-flight cap) carry a lease and
-- are taken with FOR UPDATE SKIP LOCKED; an expired lease is free, and the token stays spent.
-- Lanes, lowest number first: sync now, webhook, poll, reconcile, backfill. Only live demand
-- competes (ADR 0005 section 8, amended 2026-11-10, #370): a lane and account that asked within
-- 10 seconds and has not been granted since (it is still waiting), and the caller itself. The
-- highest live lane wins; a lane whose job stopped asking holds nothing. Same lane is
-- round-robin. One account's backfill stops at a quarter of the budget in a minute while
-- another account's live backfill is still under that share. A 601 pauses the bucket; the wait
-- is capped at 15 minutes.
--
-- No advisory lock: the bucket row is the mutex (ADR 0005 section 9, namespaces 1, 3 and 4
-- stay what they are). 20-queue.sql admits these three functions by name and nothing else.
-- The claim reads the clock after it holds the bucket row, and refilled_at never moves
-- backwards (#470): otherwise every claim that waited for the lock earned a refill again.

DO $$
BEGIN
  IF to_regclass('public.withings_rate_buckets') IS NULL
     OR to_regclass('public.withings_rate_slots') IS NULL
     OR to_regclass('public.withings_rate_demand') IS NULL THEN
    RAISE EXCEPTION 'migrate to head first';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_app') THEN
    RAISE EXCEPTION 'ownvitals_app does not exist; run docker/postgres/init/10-roles.sql first';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'ownvitals_owner') THEN
    RAISE EXCEPTION 'ownvitals_owner does not exist; run docker/postgres/init/10-roles.sql first';
  END IF;
END $$;

CREATE OR REPLACE FUNCTION public.withings_rate_claim(
    p_app_id integer, p_account_id uuid, p_lane integer, p_lease_seconds integer)
  RETURNS TABLE (granted boolean, lease_id uuid, retry_at timestamp with time zone, reason text)
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
  SET TimeZone TO 'UTC'
AS $fn$
DECLARE
  v_now timestamp with time zone := pg_catalog.clock_timestamp();
  v_budget integer;
  v_remaining numeric;
  v_refilled timestamp with time zone;
  v_paused timestamp with time zone;
  v_elapsed numeric;
  v_cap integer;
  v_winner_account uuid;
  v_winner_lane smallint;
  v_slot smallint;
  v_lease uuid;
BEGIN
  IF p_app_id IS NULL OR p_account_id IS NULL OR p_lane IS NULL OR p_lane < 1 OR p_lane > 5
     OR p_lease_seconds IS NULL OR p_lease_seconds < 1 OR p_lease_seconds > 600 THEN
    RAISE EXCEPTION 'invalid rate claim' USING ERRCODE = '22023';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.withings_apps a WHERE a.id = p_app_id) THEN
    RAISE EXCEPTION 'unknown app' USING ERRCODE = '22023';
  END IF;

  INSERT INTO public.withings_rate_buckets (app_id, remaining, budget, refilled_at)
  VALUES (p_app_id, 100, 100, v_now)
  ON CONFLICT (app_id) DO NOTHING;

  SELECT b.budget, b.remaining, b.refilled_at, b.paused_until
    INTO v_budget, v_remaining, v_refilled, v_paused
    FROM public.withings_rate_buckets b
   WHERE b.app_id = p_app_id
     FOR UPDATE;
  -- The clock is read again now that this claim holds the bucket (#470): a time read before a
  -- wait for the lock is older than the holder's refilled_at, and would earn that refill again.
  v_now := pg_catalog.clock_timestamp();

  INSERT INTO public.withings_rate_demand (
      app_id, account_id, lane, seen_at, window_grants, window_started)
  VALUES (p_app_id, p_account_id, p_lane, v_now, 0, v_now)
  ON CONFLICT (app_id, account_id, lane) DO UPDATE
    SET seen_at = GREATEST(public.withings_rate_demand.seen_at, EXCLUDED.seen_at);

  DELETE FROM public.withings_rate_demand d
   WHERE d.app_id = p_app_id AND d.seen_at < v_now - interval '2 minutes';

  IF v_paused IS NOT NULL AND v_paused > v_now THEN
    granted := false;
    lease_id := NULL;
    retry_at := v_paused;
    reason := 'paused';
    RETURN NEXT;
    RETURN;
  END IF;

  v_elapsed := extract(epoch FROM (v_now - v_refilled));
  IF v_elapsed < 0 THEN
    v_elapsed := 0;
  END IF;
  v_remaining := least(
    v_budget::numeric, v_remaining + v_elapsed * v_budget / 60);
  -- refilled_at never moves backwards (#470), even if the clock does.
  UPDATE public.withings_rate_buckets b
     SET remaining = v_remaining, refilled_at = GREATEST(v_refilled, v_now), paused_until = NULL
   WHERE b.app_id = p_app_id;

  v_cap := greatest(1, v_budget / 4);
  SELECT d.account_id, d.lane INTO v_winner_account, v_winner_lane
    FROM public.withings_rate_demand d
   WHERE d.app_id = p_app_id
     AND ((d.account_id = p_account_id AND d.lane = p_lane)
          OR (d.seen_at >= v_now - interval '10 seconds'
              AND (d.granted_at IS NULL OR d.seen_at > d.granted_at)))
     AND NOT (
       d.lane = 5
       AND d.window_started > v_now - interval '60 seconds'
       AND d.window_grants >= v_cap
       AND EXISTS (
         SELECT 1 FROM public.withings_rate_demand o
          WHERE o.app_id = d.app_id AND o.lane = 5 AND o.account_id <> d.account_id
            AND ((o.account_id = p_account_id AND o.lane = p_lane)
                 OR (o.seen_at >= v_now - interval '10 seconds'
                     AND (o.granted_at IS NULL OR o.seen_at > o.granted_at)))
            AND (o.window_started <= v_now - interval '60 seconds'
                 OR o.window_grants < v_cap)))
   ORDER BY d.lane, d.granted_at NULLS FIRST, d.account_id
   LIMIT 1;

  IF v_winner_account IS DISTINCT FROM p_account_id
     OR v_winner_lane::integer IS DISTINCT FROM p_lane THEN
    granted := false;
    lease_id := NULL;
    retry_at := v_now + interval '0.2 seconds';
    reason := 'wait';
    RETURN NEXT;
    RETURN;
  END IF;

  IF v_remaining < 1 THEN
    granted := false;
    lease_id := NULL;
    retry_at := v_now + pg_catalog.make_interval(
      secs => ((1 - v_remaining) * 60 / v_budget)::double precision);
    reason := 'empty';
    RETURN NEXT;
    RETURN;
  END IF;

  SELECT s.id INTO v_slot
    FROM public.withings_rate_slots s
   WHERE s.lease_until IS NULL OR s.lease_until <= v_now
   ORDER BY s.id
   LIMIT 1
     FOR UPDATE SKIP LOCKED;
  IF NOT FOUND THEN
    granted := false;
    lease_id := NULL;
    retry_at := v_now + interval '0.2 seconds';
    reason := 'busy';
    RETURN NEXT;
    RETURN;
  END IF;

  v_lease := pg_catalog.gen_random_uuid();
  UPDATE public.withings_rate_slots s
     SET lease_id = v_lease,
         lease_until = v_now + pg_catalog.make_interval(secs => p_lease_seconds),
         app_id = p_app_id
   WHERE s.id = v_slot;
  UPDATE public.withings_rate_buckets b
     SET remaining = v_remaining - 1
   WHERE b.app_id = p_app_id;
  UPDATE public.withings_rate_demand d
     SET granted_at = GREATEST(d.granted_at, v_now),
         window_grants = CASE
           WHEN d.window_started <= v_now - interval '60 seconds' THEN 1
           ELSE d.window_grants + 1 END,
         window_started = CASE
           WHEN d.window_started <= v_now - interval '60 seconds' THEN v_now
           ELSE d.window_started END
   WHERE d.app_id = p_app_id AND d.account_id = p_account_id AND d.lane = p_lane;

  granted := true;
  lease_id := v_lease;
  retry_at := NULL;
  reason := 'granted';
  RETURN NEXT;
END
$fn$;

CREATE OR REPLACE FUNCTION public.withings_rate_release(p_lease_id uuid)
  RETURNS void
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
  SET TimeZone TO 'UTC'
AS $fn$
BEGIN
  IF p_lease_id IS NULL THEN
    RAISE EXCEPTION 'invalid rate release' USING ERRCODE = '22023';
  END IF;
  -- The slot is free. The token is not returned: the call may already have started.
  UPDATE public.withings_rate_slots s
     SET lease_id = NULL, lease_until = NULL, app_id = NULL
   WHERE s.lease_id = p_lease_id;
END
$fn$;

CREATE OR REPLACE FUNCTION public.withings_rate_pause(
    p_app_id integer, p_wait_seconds double precision)
  RETURNS timestamp with time zone
  LANGUAGE plpgsql
  SECURITY DEFINER
  SET search_path = pg_catalog, pg_temp
  SET TimeZone TO 'UTC'
AS $fn$
DECLARE
  v_now timestamp with time zone := pg_catalog.clock_timestamp();
  v_wait double precision;
  v_until timestamp with time zone;
BEGIN
  IF p_app_id IS NULL THEN
    RAISE EXCEPTION 'invalid rate pause' USING ERRCODE = '22023';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM public.withings_apps a WHERE a.id = p_app_id) THEN
    RAISE EXCEPTION 'unknown app' USING ERRCODE = '22023';
  END IF;
  -- A non-positive or NaN wait is 60 seconds. Infinity and anything over 15 minutes
  -- are 15 minutes, so one 601 cannot hold the app's bucket forever.
  IF p_wait_seconds IS NULL OR p_wait_seconds <= 0
     OR p_wait_seconds = 'NaN'::double precision THEN
    v_wait := 60;
  ELSIF p_wait_seconds = 'Infinity'::double precision OR p_wait_seconds > 900 THEN
    v_wait := 900;
  ELSE
    v_wait := p_wait_seconds;
  END IF;

  INSERT INTO public.withings_rate_buckets (app_id, remaining, budget, refilled_at)
  VALUES (p_app_id, 100, 100, v_now)
  ON CONFLICT (app_id) DO NOTHING;

  SELECT greatest(
           coalesce(b.paused_until, v_now),
           v_now + pg_catalog.make_interval(secs => v_wait))
    INTO v_until
    FROM public.withings_rate_buckets b
   WHERE b.app_id = p_app_id
     FOR UPDATE;
  UPDATE public.withings_rate_buckets b
     SET paused_until = v_until
   WHERE b.app_id = p_app_id;
  RETURN v_until;
END
$fn$;

ALTER FUNCTION public.withings_rate_claim(integer, uuid, integer, integer)
  OWNER TO ownvitals_owner;
ALTER FUNCTION public.withings_rate_release(uuid) OWNER TO ownvitals_owner;
ALTER FUNCTION public.withings_rate_pause(integer, double precision) OWNER TO ownvitals_owner;

REVOKE ALL ON FUNCTION public.withings_rate_claim(integer, uuid, integer, integer)
  FROM PUBLIC, ownvitals_app;
REVOKE ALL ON FUNCTION public.withings_rate_release(uuid) FROM PUBLIC, ownvitals_app;
REVOKE ALL ON FUNCTION public.withings_rate_pause(integer, double precision)
  FROM PUBLIC, ownvitals_app;

-- The grantor is the function owner, whether this script runs as that role or as a superuser.
SET ROLE ownvitals_owner;
GRANT EXECUTE ON FUNCTION public.withings_rate_claim(integer, uuid, integer, integer)
  TO ownvitals_app;
GRANT EXECUTE ON FUNCTION public.withings_rate_release(uuid) TO ownvitals_app;
GRANT EXECUTE ON FUNCTION public.withings_rate_pause(integer, double precision)
  TO ownvitals_app;
RESET ROLE;

COMMENT ON FUNCTION public.withings_rate_claim(integer, uuid, integer, integer)
  IS 'ownvitals-rate v3';
COMMENT ON FUNCTION public.withings_rate_release(uuid) IS 'ownvitals-rate v3';
COMMENT ON FUNCTION public.withings_rate_pause(integer, double precision)
  IS 'ownvitals-rate v3';

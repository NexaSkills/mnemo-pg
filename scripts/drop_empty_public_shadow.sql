-- 2026-10-30: the vendored core runs module-level CREATE INDEX statements
-- WITHOUT schema qualification (beam.py idx_me_timestamp etc.). On
-- mnemo-pg hosts the profile schema is FIRST on search_path, but a
-- legacy 'public' table shadows nothing only if public lacks the table —
-- any stale public.* twin (or search_path edge) makes the core's
-- unqualified DDL hit the wrong schema. public must hold ZERO app
-- tables: the real stores live in mnemosyne_*.
DO $$
DECLARE r record; n bigint;
BEGIN
  FOR r IN SELECT c.relname AS t FROM pg_class c
           JOIN pg_namespace ns ON ns.oid = c.relnamespace
           WHERE ns.nspname = 'public' AND c.relkind = 'r'
             AND c.relname <> 'spatial_ref_sys'
  LOOP
    EXECUTE format('SELECT count(*) FROM public.%I', r.t) INTO n;
    IF n = 0 THEN
      EXECUTE format('DROP TABLE public.%I', r.t);
      RAISE NOTICE 'dropped empty public.%', r.t;
    ELSE
      RAISE NOTICE 'KEPT public.% rows=% — MANUAL REVIEW (data here is orphaned from any profile schema)', r.t, n;
    END IF;
  END LOOP;
END $$;

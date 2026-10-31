-- 2026-10-30: the vendored core's recall/consolidation SQL uses bare
-- min(a,b)/max(a,b) (SQLite 2-arg scalar forms). PG's min/max are
-- aggregates -> 'function max(double precision, double precision) does
-- not exist'. The runtime fix is a schema-local scalar function (the
-- adapter's _ensure_max_fn creates it lazily per schema, once per
-- process); provisioning it here makes fresh AND legacy schemas correct
-- at rest instead of papering over it at first connect.
-- CREATE OR REPLACE + no DROP: never touches any real aggregate.
DO $mmfunc$
DECLARE s text;
BEGIN
  FOR s IN SELECT nspname FROM pg_namespace
           WHERE nspname LIKE 'mnemosyne!_%' ESCAPE '!'
  LOOP
    BEGIN
      -- inner body carries no single quotes -> embed raw; the outer
      -- EXECUTE string wraps it in one pair (format's %% -> % literal)
      EXECUTE format('CREATE OR REPLACE FUNCTION %I.max('
                     'a double precision, b double precision) '
                     'RETURNS double precision AS $$ SELECT CASE '
                     'WHEN a IS NULL THEN coalesce(b, a) '
                     'WHEN b IS NULL THEN a '
                     'WHEN a >= b THEN a ELSE b END $$ LANGUAGE sql IMMUTABLE', s);
      EXECUTE format('CREATE OR REPLACE FUNCTION %I.min('
                     'a double precision, b double precision) '
                     'RETURNS double precision AS $$ SELECT CASE '
                     'WHEN a IS NULL THEN coalesce(b, a) '
                     'WHEN b IS NULL THEN a '
                     'WHEN a <= b THEN a ELSE b END $$ LANGUAGE sql IMMUTABLE', s);
    EXCEPTION WHEN others THEN
      RAISE NOTICE 'scalar min/max skipped for %: %', s, SQLERRM;
    END;
  END LOOP;
END $mmfunc$;

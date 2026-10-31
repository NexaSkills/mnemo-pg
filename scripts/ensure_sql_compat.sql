-- Deploy-time twin of ensure_scalar_minmax.sql, extended: creates the
-- SQLite-compat scalar functions the vendored core's SQL uses (2-arg
-- max/min, string-form datetime/strftime/julianday) AND the schema-local
-- boolean<->integer comparison operators (core SQL compares `pinned = 0`
-- and legacy native_ddl typed pinned as boolean).
-- Idempotent (CREATE OR REPLACE + guarded CREATE OPERATOR), schema-local
-- (invisible outside mnemosyne_*), never touches pg_catalog.
DO $sqlcompat$
DECLARE s text;
BEGIN
  FOR s IN SELECT nspname FROM pg_namespace
           WHERE nspname LIKE 'mnemosyne!_%' ESCAPE '!'
  LOOP
    BEGIN
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
      -- datetime(text): core calls it with ONE text arg; NULL means
      -- "unparseable" to the core's IS NULL filters, so garbage must
      -- return NULL, not raise. $q$ dollar-quoting avoids the quote
      -- maze inside the outer format() string.
      EXECUTE format($q$CREATE OR REPLACE FUNCTION %1$I.datetime(t text)
                     RETURNS text AS $$ SELECT CASE
                       WHEN t IS NULL THEN NULL
                       WHEN lower(trim(t)) IN ('now', 'subsec')
                         THEN to_char(now() at time zone 'utc',
                                      'YYYY-MM-DD HH24:MI:SS')
                       WHEN trim(t) ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}'
                         THEN to_char(trim(t)::timestamptz
                                      at time zone 'utc',
                                      'YYYY-MM-DD HH24:MI:SS')
                       ELSE NULL
                     END $$ LANGUAGE sql STABLE$q$, s);
      EXECUTE format('CREATE OR REPLACE FUNCTION %I.mnemo_bool_eq_int('
                     'a boolean, b integer) RETURNS boolean AS $$ '
                     'SELECT a::int = b $$ LANGUAGE sql IMMUTABLE', s);
      EXECUTE format('CREATE OR REPLACE FUNCTION %I.mnemo_bool_ne_int('
                     'a boolean, b integer) RETURNS boolean AS $$ '
                     'SELECT a::int <> b $$ LANGUAGE sql IMMUTABLE', s);
      EXECUTE format('DO $inner$ BEGIN '
                     'CREATE OPERATOR %I.= (LEFTARG = boolean, '
                     'RIGHTARG = integer, '
                     'FUNCTION = %I.mnemo_bool_eq_int); '
                     'EXCEPTION WHEN others THEN NULL; '
                     'END $inner$', s, s);
      EXECUTE format('DO $inner$ BEGIN '
                     'CREATE OPERATOR %I.<> (LEFTARG = boolean, '
                     'RIGHTARG = integer, '
                     'FUNCTION = %I.mnemo_bool_ne_int, '
                     'NEGATOR = =); '
                     'EXCEPTION WHEN others THEN NULL; '
                     'END $inner$', s, s);
    EXCEPTION WHEN others THEN
      RAISE NOTICE 'sql-compat skipped for %: %', s, SQLERRM;
    END;
  END LOOP;
END $sqlcompat$;

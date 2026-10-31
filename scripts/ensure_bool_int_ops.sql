-- 2026-10-31 (kim): the vendored core compares SQLite-shaped flags
-- literally:  AND (pinned IS NULL OR pinned = 0).  On PostgreSQL a
-- boolean column has no `boolean = integer` operator, so schemas whose
-- pinned column is boolean (legacy native_ddl 'boolean NOT NULL' lineage)
-- crash at the first remember() with
-- 'operator does not exist: boolean = integer'.
--
-- Schema-local operator family for boolean-vs-integer comparisons.
-- Same placement rule as the scalar min/max functions: functions and
-- operators resolve through search_path, so defining them inside each
-- mnemosyne schema is enough (the core's connects pin search_path there)
-- and NOTHING outside these schemas can see them. CREATE OR REPLACE on
-- the function + DROP/CREATE on the operator: idempotent, never touches
-- pg_catalog.
--
-- This is the backstop for data written by a schema still on the boolean
-- shape; converge_nullable_default_shape.py migrates pinned to the
-- canonical bigint shape (the primary fix).
DO $boolintop$
DECLARE s text;
BEGIN
  FOR s IN SELECT nspname FROM pg_namespace
           WHERE nspname LIKE 'mnemosyne!_%' ESCAPE '!'
  LOOP
    BEGIN
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
      RAISE NOTICE 'boolean=int operator skipped for %: %', s, SQLERRM;
    END;
  END LOOP;
END $boolintop$;

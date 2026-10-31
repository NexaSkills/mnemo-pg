-- 2026-10-30: core's _add_column_if_missing VALIDATES existing columns
-- (beam.py schema mismatch '<table>.<col>: expected <ddl>'). Legacy
-- native_ddl-lineage schemas must satisfy the full declared shape, not
-- just column existence. Local probe found 9 divergences on
-- mnemosyne_default/working_memory alone. Repair generically across all
-- mnemosyne_* schemas.
--
-- Rule applied here (conservative): where the core declares
-- INTEGER|REAL|TEXT ... DEFAULT <literal> and the column exists but has
-- no default (or wrong type), set the type + default. NOT NULL is NOT
-- added (an existing table's rows might violate it; the core's check
-- only reads type + default + notnull from PRAGMA-equivalent info — we
-- relax in the safe direction only if the core complains, per iteration).
DO $$
DECLARE
  s text;
  coltype text;
  coldefault text;
  fix_type text;
  fix_default text;
  dtbl text; dcol text; dtyp text; ddflt text; d text;
  decls text[] := ARRAY[
    -- table|column|type|default|notnull  (from the vendored core's own _add_column_if_missing calls)
    'working_memory|recall_count|bigint|0|false',
    'working_memory|last_recalled|bigint||false',
    'working_memory|binary_vector|text||false',
    'working_memory|valid_until|text||false',
    'working_memory|superseded_by|bigint||false',
    'episodic_memory|recall_count|bigint|0|false',
    'episodic_memory|last_recalled|bigint||false',
    'episodic_memory|corrected_by|bigint||false'
  ];
BEGIN
  FOR s IN SELECT n.nspname FROM pg_namespace n
           JOIN pg_class c2 ON c2.relnamespace = n.oid
           WHERE c2.relname = 'canonical_facts'
             AND n.nspname LIKE 'mnemosyne!_%' ESCAPE '!'
  LOOP
    FOREACH d IN ARRAY decls LOOP
      dtbl := split_part(d, '|', 1);
      dcol := split_part(d, '|', 2);
      dtyp := split_part(d, '|', 3);
      ddflt := split_part(d, '|', 4);
      SELECT data_type, coalesce(column_default, '')
        INTO coltype, coldefault
      FROM information_schema.columns
      WHERE table_schema = s AND table_name = dtbl AND column_name = dcol;
      IF NOT FOUND THEN
        BEGIN
          EXECUTE format('ALTER TABLE %I.%I ADD COLUMN %I %s%s',
                         s, dtbl, dcol, dtyp,
                         CASE WHEN ddflt <> '' THEN ' DEFAULT ' || ddflt ELSE '' END);
        EXCEPTION WHEN others THEN NULL;  -- table absent: not ours
        END;
        CONTINUE;
      END IF;
      fix_type := CASE WHEN coltype <> dtyp THEN dtyp ELSE NULL END;
      fix_default := CASE WHEN coldefault = '' AND ddflt <> '' THEN ddflt
                         WHEN coldefault <> '' AND ddflt <> ''
                              AND coldefault <> ddflt
                              AND coldefault !~ ('^' || ddflt)
                         THEN ddflt
                         ELSE NULL END;
      IF fix_type IS NOT NULL OR fix_default IS NOT NULL THEN
        BEGIN
          IF fix_type IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %I.%I ALTER COLUMN %I TYPE %s USING %I::%s',
                           s, dtbl, dcol, fix_type, dcol, fix_type);
          END IF;
          IF fix_default IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %I.%I ALTER COLUMN %I SET DEFAULT %s',
                           s, dtbl, dcol, fix_default);
          END IF;
          RAISE NOTICE 'fixed %.%.%: type=% default=%', s, dtbl, dcol, fix_type, fix_default;
        EXCEPTION WHEN others THEN
          RAISE NOTICE 'fix failed %.%.%: %', s, dtbl, dcol, SQLERRM;
        END;
      END IF;
    END LOOP;
  END LOOP;
END $$;

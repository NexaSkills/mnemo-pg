-- episodic_memory.summary / summary_tsv missing in drifted schemas while the
-- provisioned summary-FTS trigger (episodic_memory_summary_tsv_ts_ins, from
-- native_ddl TSVECTOR_COLS) still exists: every episodic INSERT dies with
-- 'record "new" has no field "summary_tsv"' (drift: old core CREATE TABLE
-- with no summary column + trigger added by a later provision run).
-- Repair BOTH directions idempotently:
--   A) trigger present, column missing -> add the generated tsvector column
--      (generated rather than trigger-maintained so any future trigger that
--      assigns NEW.summary_tsv cannot conflict with it)
--   B) summary_tsv present but summary missing (the NEW-assignment crash
--      shape seen on mnemosyne_default) -> add summary text; if a trigger
--      assigns NEW.summary_tsv directly, drop that trigger (the generated
--      column takes over) — otherwise a BEFORE trigger would override the
--      generated value with the error.
DO $tsvfix$
DECLARE s text; has_col boolean; has_sum boolean; trig text; fns text;
BEGIN
  FOR s IN
    SELECT n.nspname FROM pg_namespace n
    JOIN pg_class t ON t.relnamespace = n.oid AND t.relname = 'episodic_memory'
    WHERE n.nspname LIKE 'mnemosyne!_%' ESCAPE '!'
  LOOP
    SELECT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid =
      (t.oid) AND a.attname = 'summary_tsv' AND a.attnum > 0 AND NOT a.attisdropped)
      INTO has_col
      FROM pg_class t JOIN pg_namespace n ON n.oid = t.relnamespace
       AND n.nspname = s AND t.relname = 'episodic_memory';
    SELECT EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid =
      (t.oid) AND a.attname = 'summary' AND a.attnum > 0 AND NOT a.attisdropped)
      INTO has_sum
      FROM pg_class t JOIN pg_namespace n ON n.oid = t.relnamespace
       AND n.nspname = s AND t.relname = 'episodic_memory';

    IF NOT has_col THEN
      IF NOT has_sum THEN
        EXECUTE format('ALTER TABLE %I.episodic_memory ADD COLUMN IF NOT EXISTS '
                       'summary text', s);
        RAISE NOTICE 'TSVCOL %: added summary', s;
      END IF;
      EXECUTE format('ALTER TABLE %I.episodic_memory ADD COLUMN IF NOT EXISTS '
                     'summary_tsv tsvector GENERATED ALWAYS AS '
                     '(to_tsvector(''english'', COALESCE(summary, '''')) ) STORED', s);
      RAISE NOTICE 'TSVCOL %: added generated summary_tsv', s;
    END IF;

    -- any trigger whose function assigns NEW.summary_tsv is incompatible
    -- with the generated column and must go
    FOR trig IN
      SELECT tg.tgname FROM pg_trigger tg
      JOIN pg_class t ON t.oid = tg.tgrelid AND t.relname = 'episodic_memory'
      JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = s
      JOIN pg_proc p ON p.oid = tg.tgfoid
      WHERE NOT tg.tgisinternal
        AND pg_get_functiondef(p.oid) ILIKE '%NEW.summary_tsv%'
    LOOP
      EXECUTE format('DROP TRIGGER %I ON %I.episodic_memory', trig, s);
      RAISE NOTICE 'TSVCOL %: dropped trigger % (conflicts with generated summary_tsv)', s, trig;
    END LOOP;
  END LOOP;
END
$tsvfix$;

#!/usr/bin/env bash
# One-time GIN-index creation for every mnemosyne_* schema (idempotent).
# The GIN index name must be exactly what native_ddl.py creates, so
# check_shape.py sees it: gin_<table>_<tsvcol>.
set -euo pipefail
PSQL="psql -h $HOME/lcm-pg/run -p 54329 -U lcm01 -d lcm -qAc"
$PSQL >/dev/null <<'SQL'
DO $$
DECLARE s text;
BEGIN
  FOR s IN SELECT schema_name FROM information_schema.schemata
           WHERE schema_name LIKE 'mnemosyne%' LOOP
    BEGIN
      EXECUTE format('CREATE INDEX IF NOT EXISTS gin_working_memory_content_tsv ON %I.working_memory USING gin (content_tsv)', s);
      EXECUTE format('CREATE INDEX IF NOT EXISTS gin_episodic_memory_content_tsv ON %I.episodic_memory USING gin (content_tsv)', s);
      EXECUTE format('CREATE INDEX IF NOT EXISTS gin_facts_object_tsv ON %I.facts USING gin (object_tsv)', s);
      RAISE NOTICE 'GIN ok: %', s;
    EXCEPTION WHEN undefined_column OR undefined_table THEN
      RAISE NOTICE 'GIN skipped %: %', s, SQLERRM;
    END;
  END LOOP;
END $$;
SQL
echo "GIN pass done"

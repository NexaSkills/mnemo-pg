-- 2026-10-30: legacy native_ddl-lineage schemas must converge to the
-- vendored core's CREATE contract so provider init survives
-- (beam.py _init_beam_locked runs its CREATE/INDEX statements on every
-- connect; a missing column = UndefinedColumn = _beam None =
-- "memory_unavailable" on every tool call).
-- Idempotent: adds columns / backfills / relaxes NOT NULLs only where
-- divergent. Shapes mirror the healthy reference schemas (all text,
-- timestamptz for *_at columns).
DO $$
DECLARE s text;
BEGIN
  FOR s IN SELECT n.nspname FROM pg_namespace n
           JOIN pg_class c ON c.relnamespace = n.oid
           WHERE c.relname = 'canonical_facts'
             AND n.nspname LIKE 'mnemosyne!_%' ESCAPE '!'
  LOOP
    -- 1. relax legacy NOT NULLs the core's INSERTs do not satisfy
    BEGIN
      EXECUTE format('ALTER TABLE %I.canonical_facts ALTER COLUMN profile_id DROP NOT NULL', s);
    EXCEPTION WHEN others THEN NULL;
    END;
    BEGIN
      EXECUTE format('ALTER TABLE %I.working_memory ALTER COLUMN session_id DROP NOT NULL', s);
    EXCEPTION WHEN others THEN NULL;
    END;
    BEGIN
      EXECUTE format('ALTER TABLE %I.episodic_memory ALTER COLUMN session_id DROP NOT NULL', s);
    EXCEPTION WHEN others THEN NULL;
    END;

    -- 2. memory_events (sync table, beam.py 1546): core contract columns
    BEGIN
      EXECUTE format('CREATE TABLE IF NOT EXISTS %I.memory_events (
          event_id text PRIMARY KEY,
          memory_id text,
          operation text,
          "timestamp" text,
          device_id text,
          payload text,
          parent_event_ids text DEFAULT ''[]'',
          importance double precision,
          expiry text,
          event_hash text,
          synced_at text)', s);
      EXECUTE format('ALTER TABLE %I.memory_events
          ADD COLUMN IF NOT EXISTS memory_id text,
          ADD COLUMN IF NOT EXISTS operation text,
          ADD COLUMN IF NOT EXISTS "timestamp" text,
          ADD COLUMN IF NOT EXISTS device_id text,
          ADD COLUMN IF NOT EXISTS payload text,
          ADD COLUMN IF NOT EXISTS parent_event_ids text DEFAULT ''[]'',
          ADD COLUMN IF NOT EXISTS importance double precision,
          ADD COLUMN IF NOT EXISTS expiry text,
          ADD COLUMN IF NOT EXISTS event_hash text,
          ADD COLUMN IF NOT EXISTS synced_at text', s);
      EXECUTE format('CREATE INDEX IF NOT EXISTS idx_me_timestamp ON %I.memory_events("timestamp")', s);
      EXECUTE format('CREATE INDEX IF NOT EXISTS idx_me_memory_id ON %I.memory_events(memory_id)', s);
      EXECUTE format('CREATE INDEX IF NOT EXISTS idx_me_device_id ON %I.memory_events(device_id)', s);
    EXCEPTION WHEN others THEN
      RAISE NOTICE 'memory_events skipped for %: %', s, SQLERRM;
    END;

    -- 3. memoria_* (beam.py 1688+): core column names + backfill from
    --    legacy native_ddl twins (claim->value, pref_key/pref_value).
    BEGIN
      EXECUTE format('ALTER TABLE %I.memoria_facts
          ADD COLUMN IF NOT EXISTS key text,
          ADD COLUMN IF NOT EXISTS value text,
          ADD COLUMN IF NOT EXISTS fact_type text,
          ADD COLUMN IF NOT EXISTS "timestamp" text,
          ADD COLUMN IF NOT EXISTS session_id text,
          ADD COLUMN IF NOT EXISTS version_id bigint,
          ADD COLUMN IF NOT EXISTS message_idx bigint,
          ADD COLUMN IF NOT EXISTS previous_value text,
          ADD COLUMN IF NOT EXISTS context_snippet text,
          ADD COLUMN IF NOT EXISTS updated_msg_idx bigint,
          ADD COLUMN IF NOT EXISTS source_memory_id text,
          ADD COLUMN IF NOT EXISTS valid_to_msg_idx bigint,
          ADD COLUMN IF NOT EXISTS valid_from_msg_idx bigint', s);
      EXECUTE format('UPDATE %I.memoria_facts SET key = category WHERE key IS NULL AND category IS NOT NULL', s);
      EXECUTE format('UPDATE %I.memoria_facts SET value = claim WHERE value IS NULL AND claim IS NOT NULL', s);
      EXECUTE format('CREATE INDEX IF NOT EXISTS idx_facts_key ON %I.memoria_facts(key)', s);
    EXCEPTION WHEN others THEN
      RAISE NOTICE 'memoria_facts skipped for %: %', s, SQLERRM;
    END;
    BEGIN
      EXECUTE format('ALTER TABLE %I.memoria_timelines
          ADD COLUMN IF NOT EXISTS event_id bigint,
          ADD COLUMN IF NOT EXISTS date text,
          ADD COLUMN IF NOT EXISTS description text,
          ADD COLUMN IF NOT EXISTS source text,
          ADD COLUMN IF NOT EXISTS session_id text,
          ADD COLUMN IF NOT EXISTS message_idx bigint,
          ADD COLUMN IF NOT EXISTS source_memory_id text', s);
      EXECUTE format('UPDATE %I.memoria_timelines SET description = event WHERE description IS NULL AND event IS NOT NULL', s);
    EXCEPTION WHEN others THEN
      RAISE NOTICE 'memoria_timelines skipped for %: %', s, SQLERRM;
    END;
    BEGIN
      EXECUTE format('ALTER TABLE %I.memoria_instructions
          ADD COLUMN IF NOT EXISTS topic text,
          ADD COLUMN IF NOT EXISTS session_id text,
          ADD COLUMN IF NOT EXISTS message_idx bigint,
          ADD COLUMN IF NOT EXISTS context_snippet text,
          ADD COLUMN IF NOT EXISTS source_memory_id text', s);
    EXCEPTION WHEN others THEN
      RAISE NOTICE 'memoria_instructions skipped for %: %', s, SQLERRM;
    END;
    BEGIN
      EXECUTE format('ALTER TABLE %I.memoria_preferences
          ADD COLUMN IF NOT EXISTS preference text,
          ADD COLUMN IF NOT EXISTS topic text,
          ADD COLUMN IF NOT EXISTS evolution text,
          ADD COLUMN IF NOT EXISTS session_id text,
          ADD COLUMN IF NOT EXISTS message_idx bigint,
          ADD COLUMN IF NOT EXISTS context_snippet text,
          ADD COLUMN IF NOT EXISTS source_memory_id text', s);
      EXECUTE format('UPDATE %I.memoria_preferences SET preference = pref_value WHERE preference IS NULL AND pref_value IS NOT NULL', s);
      EXECUTE format('UPDATE %I.memoria_preferences SET topic = pref_key WHERE topic IS NULL AND pref_key IS NOT NULL', s);
    EXCEPTION WHEN others THEN
      RAISE NOTICE 'memoria_preferences skipped for %: %', s, SQLERRM;
    END;
    BEGIN
      EXECUTE format('ALTER TABLE %I.memoria_kg
          ADD COLUMN IF NOT EXISTS session_id text,
          ADD COLUMN IF NOT EXISTS message_idx bigint,
          ADD COLUMN IF NOT EXISTS source_memory_id text,
          ADD COLUMN IF NOT EXISTS confidence double precision', s);
    EXCEPTION WHEN others THEN
      RAISE NOTICE 'memoria_kg skipped for %: %', s, SQLERRM;
    END;
    BEGIN
      EXECUTE format('ALTER TABLE %I.memoria_persona
          ADD COLUMN IF NOT EXISTS content text,
          ADD COLUMN IF NOT EXISTS session_id text,
          ADD COLUMN IF NOT EXISTS promotion_reason text,
          ADD COLUMN IF NOT EXISTS source_memory_id text,
          ADD COLUMN IF NOT EXISTS created_at timestamptz,
          ADD COLUMN IF NOT EXISTS last_reinforced_at timestamptz,
          ADD COLUMN IF NOT EXISTS reinforcement_count bigint', s);
      EXECUTE format('UPDATE %I.memoria_persona SET content = fact WHERE content IS NULL AND fact IS NOT NULL', s);
      EXECUTE format('UPDATE %I.memoria_persona SET reinforcement_count = 0 WHERE reinforcement_count IS NULL', s);
    EXCEPTION WHEN others THEN
      RAISE NOTICE 'memoria_persona skipped for %: %', s, SQLERRM;
    END;
  END LOOP;
END $$;

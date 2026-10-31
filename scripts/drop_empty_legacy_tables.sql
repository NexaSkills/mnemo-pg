-- 2026-10-30: native_ddl's CREATE TABLE IF NOT EXISTS never converges a
-- legacy table that already exists (defaults/NOT NULL stay wrong), and
-- empty legacy tables are cheaper to rebuild than ALTER. Kim's
-- default/demo/goldie died on 'recall_count: expected INTEGER DEFAULT 0,
-- got INTEGER DEFAULT None' — provably empty tables (verified before
-- drop) get rebuilt by native_ddl's own contract, everything else is
-- left to canonical_lineage_fix.py's ALTER path.
DO $$
DECLARE
  s text;
  t text;
  n bigint;
  tables text[] := ARRAY[
    'working_memory','episodic_memory','memory_events','scratchpad',
    'memoria_facts','memoria_timelines','memoria_instructions',
    'memoria_preferences','memoria_kg','memoria_persona','consolidation_log',
    'memory_embeddings','memory_validations','facts','triples','gists',
    'graph_edges','annotations','sync_meta','sync_outbox_ack',
    'sync_memory_state','query_cache','cost_entries','harmonic_beliefs',
    'memory_resonance_log','media_assets','media_moments','hygiene_audit_log',
    'memories'];
BEGIN
  FOR s IN SELECT nspname FROM pg_namespace
           WHERE nspname LIKE 'mnemosyne!_%' ESCAPE '!'
  LOOP
    FOREACH t IN ARRAY tables LOOP
      IF to_regclass(s || '.' || t) IS NOT NULL THEN
        BEGIN
          EXECUTE format('SELECT count(*) FROM %I.%I', s, t) INTO n;
          IF n = 0 THEN
            EXECUTE format('DROP TABLE %I.%I', s, t);
            RAISE NOTICE 'dropped empty %.%', s, t;
          END IF;
        EXCEPTION WHEN others THEN
          RAISE NOTICE 'skip %.%: %', s, t, SQLERRM;
        END;
      END IF;
    END LOOP;
  END LOOP;
END $$;

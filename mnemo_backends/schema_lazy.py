"""Lazy CREATE TABLE statements the gen_ddl capture cannot reach.
Scraped by scripts/apply_add_columns.py conventions from
vendor/mnemosyne/core/veracity_consolidation.py (class attrs)."""
LAZY_DDL = [
    'CREATE TABLE IF NOT EXISTS memory_audit_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL, action TEXT NOT NULL, memory_id TEXT, bank TEXT, scope TEXT, profile TEXT, session_id TEXT, source_tool TEXT, tokens_used INTEGER, reason TEXT, metadata_json TEXT)',
    'CREATE TABLE IF NOT EXISTS conflict_probe_pending (fact_a_id TEXT NOT NULL, fact_b_id TEXT NOT NULL, conflict_type TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY (fact_a_id, fact_b_id, conflict_type))',
]

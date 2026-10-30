#!/usr/bin/env python3
"""native_ddl.py — Increment 1 (mnemo-pg).

DUMP-ONLY script: prints the declarative native-PG DDL for one mnemosyne
schema and exits. It provisions NOTHING (that lands with the provision script
in the next increment) and imports nothing from mnemosyne.

Design rules (Jan directive 2026-10-29 + lcm-pg native lessons):
  * No translation layer, no shims: one schema = real tables, real identity
    rowid columns, tsvector FTS columns + triggers + GIN, pgvector columns +
    HNSW. Derived structures (tsvectors, vec tables) are rebuilt from data,
    never migrated.
  * rowid contract: every data table carries
        <table>__rowid bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY
    (lcm-pg pattern). Data keeps its natural PK/keys. Migrations copy
    literals with OVERRIDING SYSTEM VALUE.
  * Identity columns have no pg_attrdef row: shape verifiers must accept
    attidentity IN ('a','b') as sequence-owned (lcm-pg lesson 2026-10-22).

Usage: native_ddl.py <schema-name> [--drop]
"""
from __future__ import annotations

import argparse
import re
import sys

TSVECTOR_COLS = {
    # table: [(tsv column, config, [source text columns], source rowid col)]
    "working_memory":  [("content_tsv",  "english", ["content"],   "working_memory__rowid")],
    "episodic_memory": [("summary_tsv",  "english", ["summary"],   "episodic_memory__rowid"),
                        ("content_tsv",  "english", ["content"],   "episodic_memory__rowid")],
    # live contract: the core's facts FTS runs on the object-arm tsvector
    # (fts_facts indexes subject/predicate/object; the maintained PG column
    # is object_tsv), NOT a fact_tsv — see scripts/heal_fts_views.py.
    "facts":           [("object_tsv",   "english", ["object"],     "facts__rowid")],
}

VECTOR_TABLES = {
    # vec_<x> rowid mirrors the base table's own rowid column (join key).
    "vec_working":  ("working_memory",  "working_memory__rowid"),
    "vec_episodes": ("episodic_memory", "episodic_memory__rowid"),
    "vec_facts":    ("facts",           "facts__rowid"),
}

# ---------------------------------------------------------------------------
# Core data tables: (name, [plain columns], natural primary key columns).
# A trailing "*tsv*" marker column is generated from TSVECTOR_COLS at render.
# ---------------------------------------------------------------------------
TABLES: list[tuple[str, list[str], list[str]]] = [
    ("working_memory", [
        "id text PRIMARY KEY",
        "content text NOT NULL",
        "source text",
        "timestamp timestamptz NOT NULL DEFAULT now()",
        "session_id text",
        "importance real",
        "metadata_json jsonb",
        "veracity text",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "content_tsv tsvector",                      # english, from content
        "memory_type text",
        "consolidated_at timestamptz",
        "consolidation_claimed_at timestamptz",
        "recall_count integer NOT NULL DEFAULT 0",
        "last_recalled timestamptz",
        "pinned boolean NOT NULL DEFAULT false",
        "valid_until timestamptz",
        "superseded_by text",
        "scope text",
        "author_id text",
        "author_type text",
        "channel_id text",
        "trust_tier text",
        "validator text",
        "validated_at timestamptz",
        "validation_count integer NOT NULL DEFAULT 0",
        "event_date date",
        "event_date_precision text",
        "temporal_tags jsonb",
        "corrected_by bigint",  # INTEGER in core guards (beam.py 2033/2037)
    ], []),                                          # PK declared inline (id)

    ("episodic_memory", [
        "id text PRIMARY KEY",
        "session_id text",
        "summary text",
        "content text",
        "entities jsonb",
        "timestamp timestamptz NOT NULL DEFAULT now()",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "consolidated_at timestamptz",
        "summary_tsv tsvector",                      # english, from summary
        "content_tsv tsvector",                      # english, from content
        "metadata_json jsonb",
        "importance real",
        "recall_count integer NOT NULL DEFAULT 0",
        "last_recalled timestamptz",
        "source_type text",
        "source_ref text",
        "scope text",
        "consolidation_claimed_at timestamptz",
    ], []),

    ("facts", [
        "fact_id text PRIMARY KEY",
        "fact text NOT NULL",
        "category text",
        "entities jsonb",
        "subject text",
        "predicate text",
        "object text",
        "session_id text",
        "source_msg_id text",
        "valid_from timestamptz",
        "valid_until timestamptz",
        "absorbed_into text",
        "triple_source text",
        "superseded_by text",
        "supersede_reason text",
        "timestamp timestamptz NOT NULL DEFAULT now()",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "object_tsv tsvector",                       # english, from object
        "importance real",
        "source text",
        "scope text",
        "channel_id text",
        "consolidation_claimed_at timestamptz",
    ], []),

    ("canonical_facts", [
        "id text PRIMARY KEY",
        "profile_id text NOT NULL",
        "category text NOT NULL",
        "name text NOT NULL",
        "body text NOT NULL",
        "source text",
        "confidence real",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "updated_at timestamptz NOT NULL DEFAULT now()",
        "valid_until timestamptz",
        "supersedes text",
        "superseded_by text",
    ], []),

    ("triples", [
        "id text PRIMARY KEY",
        "subject text NOT NULL",
        "predicate text NOT NULL",
        "object text NOT NULL",
        "valid_from timestamptz NOT NULL DEFAULT now()",
        "valid_until timestamptz",
        "source text",
        "confidence real",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "scope text",
        "channel_id text",
    ], []),

    ("graph_edges", [
        "id text PRIMARY KEY",
        "source_memory_id text NOT NULL",
        "target_memory_id text NOT NULL",
        "edge_type text NOT NULL",
        "weight real NOT NULL DEFAULT 0.5",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "scope text",
    ], []),

    ("memoria_facts", [
        "id text PRIMARY KEY",
        "claim text NOT NULL",
        "category text",
        "importance real",
        "metadata_json jsonb",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "source text",
        "scope text",
    ], []),

    ("memoria_persona", [
        "id text PRIMARY KEY",
        "fact text NOT NULL",
        "tier text",
        "topic text",
        "source_working_id text",
        "reinforcement_count integer NOT NULL DEFAULT 0",
        "last_reinforced_at timestamptz",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "scope text",
    ], []),

    ("memoria_preferences", [
        "id text PRIMARY KEY",
        "pref_key text NOT NULL",
        "pref_value text NOT NULL",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "updated_at timestamptz NOT NULL DEFAULT now()",
        "scope text",
    ], []),

    ("memoria_instructions", [
        "id text PRIMARY KEY",
        "instruction text NOT NULL",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "active boolean NOT NULL DEFAULT true",
        "scope text",
    ], []),

    ("memoria_kg", [
        "id text PRIMARY KEY",
        "subject text NOT NULL",
        "predicate text NOT NULL",
        "object text NOT NULL",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "scope text",
    ], []),

    ("memoria_timelines", [
        "id text PRIMARY KEY",
        "event text NOT NULL",
        "event_time timestamptz",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "scope text",
    ], []),

    ("scratchpad", [
        "id text PRIMARY KEY",
        "content text NOT NULL",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "session_id text",
        "scope text",
    ], []),

    ("consolidated_facts", [
        "id text PRIMARY KEY",
        "claim text NOT NULL",
        "category text",
        "supporting_ids jsonb",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "superseded_by text",
        "scope text",
    ], []),

    ("consolidation_log", [
        "id text PRIMARY KEY",
        "ran_at timestamptz NOT NULL DEFAULT now()",
        "consolidated_count integer NOT NULL DEFAULT 0",
        "detail jsonb",
        "scope text",
    ], []),

    ("conflicts", [
        "id text PRIMARY KEY",
        "left_id text NOT NULL",
        "right_id text NOT NULL",
        "status text",
        "detected_at timestamptz NOT NULL DEFAULT now()",
        "resolved_at timestamptz",
        "resolution text",
        "scope text",
    ], []),

    ("memory_validations", [
        "id text PRIMARY KEY",
        "memory_id text NOT NULL",
        "validator text",
        "action text",
        "note text",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "scope text",
    ], []),

    ("memory_audit_events", [
        "id text PRIMARY KEY",
        "event_type text NOT NULL",
        "memory_id text",
        "actor text",
        "detail jsonb",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "scope text",
    ], []),

    ("memory_events", [
        "id text PRIMARY KEY",
        "event_type text NOT NULL",
        "payload jsonb",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "scope text",
    ], []),

    ("media_assets", [
        "id text PRIMARY KEY",
        "uri text NOT NULL",
        "modality text",
        "mime text",
        "title text",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "metadata_json jsonb",
        "scope text",
    ], []),

    ("media_moments", [
        "id text PRIMARY KEY",
        "asset_id text NOT NULL",
        "kind text",
        "text text",
        "t_start_ms integer",
        "t_end_ms integer",
        "page integer",
        "importance real",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "scope text",
    ], []),

    ("gists", [
        "id text PRIMARY KEY",
        "text text NOT NULL",
        "source_span text",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "scope text",
    ], []),

    ("annotations", [
        "id text PRIMARY KEY",
        "memory_id text",
        "text text NOT NULL",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "scope text",
    ], []),

    ("audit_log", [
        "id text PRIMARY KEY",
        "action text NOT NULL",
        "actor text",
        "detail jsonb",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "scope text",
    ], []),

    ("memory_embeddings", [
        "memory_id text PRIMARY KEY",
        "embedding_json jsonb NOT NULL",
        "model text",
        "created_at timestamptz NOT NULL DEFAULT now()",
        "scope text",
    ], []),
]

# Index hints (name, table, using-clause body) — kept deliberately minimal;
# extras land with the feature-parity increment when a feature demands them.
INDEXES = [
    ("idx_wm_ts",           "working_memory",  "btree (timestamp)"),
    ("idx_wm_valid_until",  "working_memory",  "btree (valid_until) WHERE valid_until IS NOT NULL"),
    ("idx_wm_supersedes",   "working_memory",  "btree (superseded_by) WHERE superseded_by IS NOT NULL"),
    ("idx_epi_ts",          "episodic_memory", "btree (timestamp)"),
    ("idx_facts_cat",       "facts",           "btree (category)"),
    ("idx_triples_spo",     "triples",         "btree (subject, predicate, object)"),
    ("idx_triples_valid",   "triples",         "btree (subject, predicate) WHERE valid_until IS NULL"),
    ("idx_ge_source",       "graph_edges",     "btree (source_memory_id)"),
    ("idx_ge_target",       "graph_edges",     "btree (target_memory_id)"),
    ("idx_cf_slot",         "canonical_facts", "btree (profile_id, category, name)"),
    ("idx_mm_asset",        "media_moments",   "btree (asset_id)"),
]


def alter_statements(schema: str) -> list[str]:
    """ALTER statements that converge an EXISTING (legacy/shim-era) schema
    onto the native contract. One statement per table, built entirely from
    IF-[NOT-]EXISTS clauses so PG skips present objects; never touches data.
    Used by provision_schema.py; a fresh schema gets pure no-ops."""
    stmts: list[str] = []
    for name, cols, pk in TABLES:
        rowid = f"{name}__rowid"
        parts = [f"ADD COLUMN IF NOT EXISTS {rowid} bigint"]
        for c in cols:
            col_def = c.replace(" PRIMARY KEY", "")
            col_name = col_def.split()[0]
            # a serial/sequence-fed or shim-identity key column is made plain
            # first so the ADD below (no default) matches the contract shape
            parts.append(f"ALTER COLUMN {col_name} DROP DEFAULT")
            parts.append(f"ALTER COLUMN {col_name} DROP IDENTITY IF EXISTS")
            parts.append(f"ADD COLUMN IF NOT EXISTS {col_def}")
        stmts.append(
            f"ALTER TABLE {schema}.{name}\n    " + ",\n    ".join(parts))
        if pk:
            stmts.append(
                f"ALTER TABLE {schema}.{name} ADD UNIQUE IF NOT EXISTS "
                f"({', '.join(pk)})")
        for c in cols:
            if c.endswith(" PRIMARY KEY"):
                kcol = c.split()[0]
                stmts.append(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{name}_{kcol} "
                    f"ON {schema}.{name} ({kcol})")
    return stmts


def render(schema: str) -> str:
    out: list[str] = []
    a = out.append
    a(f"-- mnemosyne native schema: {schema}")
    a("-- generated by scripts/native_ddl.py — DUMP ONLY (provisioning lands in the next increment)")
    a("SET search_path TO public;")
    a(f"CREATE SCHEMA IF NOT EXISTS {schema};")
    a(f"SET search_path TO {schema}, public;")
    a("CREATE EXTENSION IF NOT EXISTS vector;")
    a("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    a("")

    for name, cols, pk in TABLES:
        rowid = f"{name}__rowid"
        body = [f"    {rowid} bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY"]
        # drop an inline PRIMARY KEY from natural cols (rowid is the PK); the
        # natural key keeps uniqueness via a UNIQUE index rendered below.
        natural = [c for c in cols]
        natural = [c.replace(" PRIMARY KEY", "") for c in natural]
        body += [f"    {c}" for c in natural]
        if pk:
            body.append(f"    UNIQUE ({', '.join(pk)})")
        a(f"CREATE TABLE IF NOT EXISTS {schema}.{name} (")
        a(",\n".join(body))
        a(");")
        # single-column natural PKs get a UNIQUE index for ON CONFLICT (col)
        for c in cols:
            if c.endswith(" PRIMARY KEY"):
                kcol = c.split()[0]
                a(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{name}_{kcol} "
                  f"ON {schema}.{name} ({kcol});")
        a("")

    # FTS: tsvector columns above; triggers + GIN here.
    a("-- FTS: trigger-maintained tsvector columns + GIN indexes")
    for table, specs in TSVECTOR_COLS.items():
        rowid = f"{table}__rowid"
        for tcol, cfg, sources, _ in specs:
            concat = " || ' ' || ".join(f"COALESCE(NEW.{s}, '')" for s in sources)
            fn = f"{table}_{tcol}_ts"
            a(f"""CREATE OR REPLACE FUNCTION {schema}.{fn}() RETURNS trigger AS $$
BEGIN
  NEW.{tcol} := to_tsvector('{cfg}', {concat});
  RETURN NEW;
END $$ LANGUAGE plpgsql;""")
            a(f"DROP TRIGGER IF EXISTS {fn}_ins ON {schema}.{table};")
            a(f"""CREATE TRIGGER {fn}_ins BEFORE INSERT OR UPDATE ON {schema}.{table}
FOR EACH ROW EXECUTE FUNCTION {schema}.{fn}();""")
            a(f"CREATE INDEX IF NOT EXISTS gin_{table}_{tcol} "
              f"ON {schema}.{table} USING gin ({tcol});")
        del rowid
    a("")

    a("-- Vectors: exact fp32 pgvector columns, HNSW cosine (int8 dropped by design)")
    for vname, (base, base_rowid) in VECTOR_TABLES.items():
        a(f"""CREATE TABLE IF NOT EXISTS {schema}.{vname} (
    rowid bigint PRIMARY KEY,
    embedding vector(384) NOT NULL,
    CONSTRAINT {vname}_rowid_fk FOREIGN KEY (rowid)
        REFERENCES {schema}.{base} ({base_rowid}) ON DELETE CASCADE
);""")
        a(f"CREATE INDEX IF NOT EXISTS hnsw_{vname} ON {schema}.{vname} "
          f"USING hnsw (embedding vector_cosine_ops);")
    a("")

    a("-- Backfill triggers for existing rows (idempotent; runs once per trigger)")
    for table, specs in TSVECTOR_COLS.items():
        for tcol, cfg, sources, _ in specs:
            concat = " || ' ' || ".join(f"COALESCE({s}, '')" for s in sources)
            a(f"UPDATE {schema}.{table} SET {tcol} = to_tsvector('{cfg}', {concat}) "
              f"WHERE {tcol} IS NULL;")
    a("")
    a("-- vec_* population is derived data: rebuilt by the sync/rebuild scripts "
      "from memory_embeddings, never dumped.")
    a("")

    # Date parity functions: SQLite julianday/strftime equivalents used by the
    # native backend's temporal queries (mnemo_backends/postgres_native.py).
    a("""CREATE OR REPLACE FUNCTION {schema}.julianday_parity(t text) RETURNS double precision AS $$
  SELECT CASE
    WHEN t IS NULL OR btrim(t) = '' THEN NULL
    WHEN t ~ '^[0-9]+(\\.[0-9]+)?$' THEN t::double precision
    ELSE extract(epoch FROM t::timestamptz) / 86400.0 + 2440587.5
  END
$$ LANGUAGE sql IMMUTABLE;""".replace("{schema}", schema))
    a("""CREATE OR REPLACE FUNCTION {schema}.strftime_parity(fmt text, t timestamptz) RETURNS text AS $$
  SELECT CASE
    WHEN t IS NULL THEN NULL
    WHEN fmt = '%Y-%m-%d' THEN to_char(t, 'YYYY-MM-DD')
    WHEN fmt = '%Y-%m' THEN to_char(t, 'YYYY-MM')
    WHEN fmt = '%Y' THEN to_char(t, 'YYYY')
    WHEN fmt = '%H:%M:%S' THEN to_char(t, 'HH24:MI:SS')
    ELSE to_char(t, replace(replace(replace(fmt, '%Y', 'YYYY'), '%m', 'MM'), '%d', 'DD'))
  END
$$ LANGUAGE sql IMMUTABLE;""".replace("{schema}", schema))

    # SQLite's 2-arg scalar MAX(a,b): PG only has the aggregate, so the
    # core's `SET importance = MAX(importance, ?)` style updates fail.
    a("""CREATE OR REPLACE FUNCTION {schema}.max(a double precision, b double precision) RETURNS double precision AS $$
  SELECT CASE
    WHEN a IS NULL THEN coalesce(b, a)
    WHEN b IS NULL THEN a
    WHEN a >= b THEN a
    ELSE b
  END
$$ LANGUAGE sql IMMUTABLE;""".replace("{schema}", schema))

    # FTS query normalizer: FTS5-style query text -> tsquery lexeme text.
    # Same contract the shim's helper had, defined in native schema space.
    # NOTE: backslashes here are REAL (single-quoted SQL literals inside a
    # plain Python string) — do not add Python-level escaping.
    a(r"""CREATE OR REPLACE FUNCTION {schema}.mnemo_normalize_fts(q text) RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $fn$
-- FTS5-style query text -> valid to_tsquery argument (simple config).
-- Connector words and prefix flags are masked with placeholder lexemes
-- BEFORE the lexeme sanitizer and restored AFTER; bare adjacency joins with
-- & (FTS5 default), 'tok*' becomes PG prefix form 'tok:*', NOT-only queries
-- return NULL (callers skip the leg). NOTE: this Python string is RAW so
-- backslashes reach SQL as single real characters.
DECLARE s text;
BEGIN
    s := coalesce(q, '');
    IF btrim(s) = '' THEN RETURN NULL; END IF;
    s := lower(s);
    s := regexp_replace(s, '\mnot\y',  ' zz0zz ', 'g');
    s := regexp_replace(s, '\mor\y',   ' zz1zz ', 'g');
    s := regexp_replace(s, '\mand\y',  ' zz2zz ', 'g');
    s := replace(s, '|', ' zz1zz ');                       -- FTS5 pipe == OR
    s := replace(s, '!', ' zz0zz ');                       -- FTS5 minus == NOT
    s := replace(s, '"', ' ');                             -- phrase -> words
    s := regexp_replace(s, '\m[a-z]+\s*/\s*[0-9]+\y', ' zz1zz ', 'g'); -- NEAR/n
    s := regexp_replace(s, '([0-9a-z_]+)\*', '\1:zz4zz', 'g');         -- prefix flag
    s := replace(s, '*', '');                              -- stray prefix stars
    s := regexp_replace(s, '[^0-9a-z_:]+', ' ', 'g');      -- lexemes + markers
    s := regexp_replace(s, '\s+', ' ', 'g');
    s := btrim(s);
    -- join runs of bare lexemes (no marker inside the run) with &
    WHILE s ~ '(^|[ ])(?!zz)[0-9a-z_:]+ +(?!zz)[0-9a-z_:]+([ ]|zz|$)' LOOP
        s := regexp_replace(s, '(^|[ ])((?!zz)[0-9a-z_:]+) +((?!zz)[0-9a-z_:]+)(?![0-9a-z_:])', '\1\2 & \3', 'g');
    END LOOP;
    -- restore connectors (FTS5 & AND ! are BINARY operators in PG: ! binds
    -- like AND; "a NOT b" -> "a & !b")
    s := regexp_replace(s, '\s*zz1zz\s*', ' | ', 'g');     -- OR
    s := regexp_replace(s, '\s*zz0zz\s*', ' & ! ', 'g');   -- NOT
    s := regexp_replace(s, '\s*zz2zz\s*', ' & ', 'g');     -- AND
    s := regexp_replace(s, ':zz4zz', ':*', 'g');           -- prefix flag back
    -- operator pileups collapse (rightmost operator wins where ambiguous)
    s := regexp_replace(s, '(&|\|)( *(((&|\|)|!)))+', '\4', 'g');
    s := regexp_replace(s, ' *! *([0-9a-z_:]+)', ' & !\1', 'g');  -- '!x' -> '& !x'
    s := regexp_replace(s, '\s+', ' ', 'g');
    s := btrim(s);
    s := regexp_replace(s, '^((&|\|)+ *(& *!)?|& *!)+ *', '', 'g'); -- leading ops
    s := regexp_replace(s, ' *((&|\|)+(& *!)?|& *!|!)+$', '', 'g'); -- trailing ops
    s := regexp_replace(s, '^!+$', '', 'g');               -- bare NOT -> empty
    s := btrim(s);
    IF s = '' OR s !~ '[0-9a-z]' THEN RETURN NULL; END IF;
    IF s ~ '^!' THEN RETURN NULL; END IF;                  -- NOT-only -> NULL
    RETURN s;
END $fn$;""".replace("{schema}", schema))
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("schema", help="schema name, e.g. mnemosyne_mike")
    args = ap.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", args.schema):
        print(f"refusing unusual schema name: {args.schema!r}", file=sys.stderr)
        return 2
    print(render(args.schema))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

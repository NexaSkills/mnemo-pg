# mnemo-pg — PostgreSQL-native mnemosyne

Mnemosyne clone native to PostgreSQL. No SQLite at runtime, no shims, no SQL
translation layers (Jan 2026-10-29: lcm-pg proved wrappers never cover all
translations; the native pivot was more reliable, elegant, and simple).

## Native contract
- One PostgreSQL schema per agent profile (mnemosyne_<profile>) + one shared
  schema (mnemosyne_shared), on the lcm cluster (PG 18.6, vector 0.8.7,
  pg_trgm 1.6; socket ~/lcm-pg/run:54329, role lcm01, db lcm).
- Every table carries a real bigint rowid:
  `<table>__rowid bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY`.
  Natural keys (text ids) stay UNIQUE for ON CONFLICT upserts.
  Gate note: identity columns have NO pg_attrdef row — shape verifiers check
  attidentity IN ('a','b') (lcm-pg lesson 2026-10-22).
- FTS: tsvector columns maintained by BEFORE INSERT/UPDATE triggers + GIN
  indexes. FTS5 query forms map natively: `a AND b` -> to_tsquery('&'), OR ->
  websearch/`||`, prefix -> lexize + ':*'. No FTS5 virtual tables exist.
- Vectors: exact fp32 pgvector `vector(384)` columns in vec_<base> with HNSW
  cosine indexes; rowid mirrors the base table (FK ON DELETE CASCADE). int8
  quantization is dropped by design (lossy; Jan confirmed 2026-10-29).
- Dates: timestamptz native; julianday parity = extract(epoch FROM x)+2440587.5.
- Derived structures (tsvectors, vec_*) are rebuilt from data, never dumped.

## Repo layout
- scripts/native_ddl.py    declarative native DDL (dump-only; proven on live PG)
- scripts/provision_schema.py  idempotent apply (next increment)
- scripts/migrate_sqlite.py    row-preserving copy (OVERRIDING SYSTEM VALUE)
- mnemo_backends/          backend factory (native path; shim code is history)
- vendor/mnemosyne/        upstream core (vendored, feature parity reference)
- plugin/                  hermes memory-provider plugin hooks
- tests/                   feature-parity matrix per profile (see below)

## Deploy (mirrors lcm-pg exactly)
mnemo_update.sh: pinned GitHub ref -> provision/migrate (idempotent) ->
config sync -> surface restarts -> mnemo_check_all.py report.
Gate: beez proves EVERY feature for ALL profiles before fleet rollout.
Feature surface: 57 mnemosyne_* tools + prefetch/sync/tools hooks.

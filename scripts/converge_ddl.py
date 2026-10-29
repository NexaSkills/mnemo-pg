#!/usr/bin/env python3
"""Per-table convergence for a legacy (shim-era) schema onto native_ddl's
contract, WITHOUT renaming natural keys.

render() calls itself 'DUMP ONLY': for an existing table its CREATE TABLE
IF NOT EXISTS is a no-op, so contract columns (the bigint <t>__rowid twin)
and uq_* indexes never land. shape_pass.py does this job at runtime; this
module mirrors the same contract declaratively so provision_schema.py can
converge legacy schemas from the DDL alone:

  1. ADD COLUMN IF NOT EXISTS <t>__rowid bigint (the contract rowid twin;
     a fresh table already got it from CREATE TABLE, a legacy one gets it here)
  2. CREATE UNIQUE INDEX IF NOT EXISTS uq_<t>_<keycol> (only when that
     column actually exists — legacy key names differ per lineage)
  3. unique-PK ADD CONSTRAINT (guarded by table existence)

Identity conversion of twin columns (plain bigint -> GENERATED) is
data-touching and belongs to scripts/shape_pass.py / retire_sequences.py,
not here. All statements are IF-[NOT-]EXISTS / guarded: no-ops on a fresh
or already-conformant schema.
"""
from __future__ import annotations


def _exists(schema: str, rel: str, col: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM pg_attribute a "
        "JOIN pg_class c ON c.oid=a.attrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        f"WHERE n.nspname='{schema}' AND c.relname='{rel}' "
        f"AND a.attname='{col}' AND a.attnum>0)")


def legacy_statements(schema: str, tables) -> list[str]:
    stmts: list[str] = []
    for name, cols, pk in tables:
        rowid = f"{name}__rowid"
        stmts.append(
            f"ALTER TABLE {schema}.{name} "
            f"ADD COLUMN IF NOT EXISTS {rowid} bigint")
        for c in cols:
            if c.endswith(" PRIMARY KEY"):
                kcol = c.split()[0]
                stmts.append(
                    "DO $$BEGIN IF " + _exists(schema, name, kcol) +
                    " THEN EXECUTE 'CREATE UNIQUE INDEX IF NOT EXISTS "
                    f"uq_{name}_{kcol} ON {schema}.{name} ({kcol})'; "
                    "END IF; END$$")
        if pk:
            pklist = ", ".join(pk)
            stmts.append(
                "DO $$BEGIN IF "
                + " AND ".join(_exists(schema, name, k) for k in pk)
                + " THEN EXECUTE format('ALTER TABLE %s ADD UNIQUE (\"\n"
                + ", ".join(f'"{k}"' for k in pk) + "\")', '" + schema + "." + name + "'); "
                "END IF; END$$")
    return stmts

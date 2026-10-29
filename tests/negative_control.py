#!/usr/bin/env python3
"""Negative-control test for scripts/check_shape.py (lcm-pg discipline).

For each defect class, provision a throwaway schema, inject the defect, and
prove check_shape FAILS with the expected violation. A gate that only ever
reports clean is worthless.

Usage: python3 tests/negative_control.py
Throws away schemas are named nctrl_<case> and dropped at the end.
"""
from __future__ import annotations

import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import psycopg  # noqa: E402
import provision_schema as ps  # noqa: E402
import check_shape as cs  # noqa: E402

CASES = {
    "missing_table":
        ("missing table", lambda c, s: c.execute(f'DROP TABLE "{s}"."scratchpad"')),
    "no_identity":
        ("no identity rowid", lambda c, s: c.execute(
            f'ALTER TABLE "{s}"."gists" ALTER COLUMN gists__rowid DROP IDENTITY')),
    "no_gin":
        ("no GIN index", lambda c, s: c.execute(
            f'DROP INDEX "{s}"."gin_working_memory_content_tsv"')),
    "no_trigger":
        ("no tsvector trigger", lambda c, s: c.execute(
            f'DROP TRIGGER "facts_fact_tsv_ts_ins" ON "{s}"."facts"')),
    "no_hnsw":
        ("no HNSW cosine index", lambda c, s: c.execute(
            f'DROP INDEX "{s}"."hnsw_vec_working"')),
    "no_fk_cascade":
        ("no ON DELETE CASCADE FK", lambda c, s: c.execute(
            f'ALTER TABLE "{s}"."vec_episodes" DROP CONSTRAINT vec_episodes_rowid_fk')),
    "no_fn":
        ("missing parity/normalize function", lambda c, s: c.execute(
            f'DROP FUNCTION "{s}"."julianday_parity"(text)')),
}


def main() -> int:
    dsn = ps.dsn()
    conn = psycopg.connect(dsn, autocommit=True)
    failures = 0
    stamp = int(time.time())
    try:
        for case, (expect, inject) in CASES.items():
            schema = f"nctrl_{case}"
            if not schema.replace("_", "").isalnum():
                schema = "nctrl_" + str(stamp)
            ps.provision(conn, schema)
            base = cs.check(conn, schema, trusted=False)
            assert not base, f"clean schema failed gate before injection: {base}"
            with conn.cursor() as cur:
                inject(cur, schema)
            bad = cs.check(conn, schema, trusted=False)
            hit = [b for b in bad if expect in b]
            if hit:
                print(f"CAUGHT {case}: {hit[0]}")
            else:
                print(f"MISSED {case}: gate said {bad or 'OK'} (expected '{expect}')")
                failures += 1
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        conn.close()
    print("negative control:", "PASS" if failures == 0 else f"FAIL ({failures} missed)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

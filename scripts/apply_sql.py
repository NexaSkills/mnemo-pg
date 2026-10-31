#!/usr/bin/env python3
"""Apply a .sql file to the lcm cluster via psycopg (no psql dependency).

Used by mnemo_update.sh for codified DO-block repairs (e.g.
fix_pinned_boolean_no_default.sql). DSN resolution mirrors the other
scripts: MNEMO_PG_DSN env, else the fleet convention (socket
~/lcm-pg/run, port 54329, role lcm01, db lcm). Statements are sent
verbatim (the files are self-contained DO blocks / ALTERs).

Usage: apply_sql.py <file.sql> [--dsn DSN]
"""
import argparse
import os
import pathlib
import sys

import psycopg
import psycopg.sql

DEFAULT_DSN = ("postgresql://lcm01@/lcm?host="
               + str(pathlib.Path.home() / "lcm-pg/run") + "&port=54329")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sql_file")
    ap.add_argument("--dsn", default=os.environ.get("MNEMO_PG_DSN", DEFAULT_DSN))
    args = ap.parse_args()
    sql = pathlib.Path(args.sql_file).read_text()
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(psycopg.sql.Composed([psycopg.sql.SQL(sql)]))
    print(f"apply-sql: {args.sql_file} applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())

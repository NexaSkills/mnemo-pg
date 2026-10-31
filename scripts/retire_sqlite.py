#!/usr/bin/env python3
"""retire_sqlite.py — archive + delete live mnemosyne SQLite files, install tripwire.

Destructive by design (Jan-approved retirement). Scope: inside ~/.hermes only.
  1. Finds live mnemosyne*.db (+ -wal/-shm) under ~/.hermes (skips backups/).
  2. HARD PARITY GATE (F-B, 2026-10-31): for each db, EVERY user table's rows
     must be present in the target PG schema — per-table row counts (PG >=
     sqlite) AND content overlap (every sqlite row matched by its first-column
     key in PG; NULL-keyed rows checked by count; tables absent from PG fail
     when the sqlite side holds rows). Any missing content => refuse: nothing
     archived, nothing deleted, exit 1.
     --force skips the gate (logged LOUDLY — it used to be a silent opt-out).
     The plan-era name gate_before_retire.py referred to a script deleted in
     4def403; the gate now lives here, inside the only script that deletes.
  3. Archives to ~/.hermes/backups/mnemosyne-sqlite-retired-<ts>/ preserving rel paths.
  4. Deletes originals.
  5. Installs mnemo-sqlite-tripwire.{timer,service} systemd-user units that alarm
     if a live mnemosyne db reappears.
Flags: --dry-run, --force (skip the parity gate), --schema NAME (override the
PG target schema; default derives from the db's path like the factory does).
"""
import argparse, datetime, os, pathlib, shutil, sqlite3, subprocess, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import provision_schema  # DSN resolution: $MNEMO_PG_DSN, .env, private cluster

HERMES = pathlib.Path.home() / ".hermes"
UNITS = pathlib.Path.home() / ".config/systemd/user"
# self-locate: this script lives in <repo>/scripts, ops units in <repo>/ops
PLUGIN_DIR = pathlib.Path(__file__).resolve().parent.parent
if os.environ.get("MNEMO_TRIPWIRE_UNITS"):
    UNITS = pathlib.Path(os.environ["MNEMO_TRIPWIRE_UNITS"])

# sqlite bookkeeping tables never mirrored to PG
SKIP_TABLES = {"sqlite_sequence"}


def find_live():
    # test seam: fake HOME stand-in. Must be EMPTY (only .db files are
    # archived) so a no-op run can clean it for the next invocation.
    base = pathlib.Path(os.environ.get("MNEMO_RETIRE_HOMES", HERMES))
    out = []
    for p in base.rglob("*.db"):
        s = str(p)
        if "mnemosyne" not in p.name.lower():
            continue
        if "/backups/" in s or "/mnemo-pg/" in s:
            continue
        if p.stat().st_size == 0:
            continue   # 0-byte probe artifacts: quarantine_residual_sqlite's job
        out.append(p)
    return sorted(out)


def schema_for(db: pathlib.Path, override: str | None) -> str:
    if override:
        return override
    if "shared" in str(db).lower():
        return os.environ.get("MNEMO_SHARED_SCHEMA", "mnemosyne_shared")
    s = str(db)
    marker = "/profiles/"
    if marker in s:
        prof = s.split(marker, 1)[1].split("/", 1)[0]
    else:
        prof = "default"
    return "mnemosyne_" + prof.lower().replace("-", "_")


def parity(db: pathlib.Path, schema: str) -> tuple[bool, list[str]]:
    """Return (ok, report lines), one line per sqlite user table."""
    import psycopg
    rows_out = []
    ok = True
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")
            if r[0] not in SKIP_TABLES]
        with psycopg.connect(provision_schema.dsn(), autocommit=True) as pg:
            pg.execute('SET search_path = "%s", public' %
                       schema.replace('"', '""'))
            have = {r[0] for r in pg.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s", (schema,))}
            for t in tables:
                n_src = src.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                if t not in have:
                    if n_src:
                        rows_out.append(f"  {t}: FAIL (table absent in PG, "
                                        f"sqlite has {n_src} rows)")
                        ok = False
                    else:
                        rows_out.append(f"  {t}: ok (empty, absent in PG)")
                    continue
                n_dst = pg.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                if n_dst < n_src:
                    rows_out.append(f"  {t}: FAIL count pg={n_dst} < "
                                    f"sqlite={n_src}")
                    ok = False
                    continue
                if n_src == 0:
                    rows_out.append(f"  {t}: ok {n_src}/{n_dst} counts")
                    continue
                cols = [r[1] for r in src.execute(f'PRAGMA table_info("{t}")')]
                kcol = cols[0]  # key-based overlap; sqlite cols lead with PK
                try:
                    keys_src = {r[0] for r in src.execute(
                        f'SELECT "{kcol}" FROM "{t}"')}
                    keys_pg = {r[0] for r in pg.execute(
                        f'SELECT "{kcol}" FROM "{t}"')}
                    missing = (keys_src - keys_pg) - {None}
                    null_src = src.execute(
                        f'SELECT COUNT(*) FROM "{t}" WHERE "{kcol}" IS NULL'
                    ).fetchone()[0]
                    null_pg = pg.execute(
                        f'SELECT COUNT(*) FROM "{t}" WHERE "{kcol}" IS NULL'
                    ).fetchone()[0]
                    n_missing = len(missing) + max(0, null_src - null_pg)
                    if n_missing:
                        rows_out.append(
                            f"  {t}: FAIL content ({n_missing} of {n_src} "
                            f"sqlite rows missing in PG by key '{kcol}')")
                        ok = False
                        continue
                    rows_out.append(f"  {t}: ok {n_src}/{n_dst} counts, "
                                    f"content overlap "
                                    f"{len(keys_src - {None})} keys")
                except Exception as exc:
                    rows_out.append(f"  {t}: FAIL parity probe error: "
                                    f"{str(exc).splitlines()[0]}")
                    ok = False
    finally:
        src.close()
    return ok, rows_out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="SKIP the parity gate (loud; audit-logged)")
    ap.add_argument("--schema", default=None,
                    help="override target PG schema for the gate")
    a = ap.parse_args()
    live = find_live()
    if not live:
        print("no live mnemosyne sqlite files found")
    # GATE FIRST: refuse everything if ANY db fails parity (nothing moves).
    gate_fail = False
    for db in live:
        schema = schema_for(db, a.schema)
        print(f"gate: {db} -> PG schema {schema}")
        if a.force:
            print("  FORCE: parity gate SKIPPED (audit: operator override)")
            continue
        try:
            ok, rows = parity(db, schema)
        except Exception as exc:
            print(f"  FAIL gate cannot run: {exc}")
            gate_fail = True
            continue
        for line in rows:
            print(line)
        print(f"  {'PASS' if ok else 'FAIL'}")
        gate_fail = gate_fail or not ok
    if gate_fail:
        print("GATE: FAIL — refusing to retire. Content is missing in PG; "
              "migrate/repair first, or --force to bypass (loud).")
        return 1
    print("GATE: PASS" if live else "GATE: PASS (nothing to gate)")
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    arch = HERMES / "backups" / f"mnemosyne-sqlite-retired-{ts}"
    for db in live:
        for side in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            if not side.exists():
                continue
            try:
                rel = side.relative_to(HERMES)
            except ValueError:
                rel = pathlib.Path(str(side).lstrip("/").replace("/", "_"))
            dest = arch / rel
            print(f"{'DRY ' if a.dry_run else ''}archive {side} -> {dest}")
            if not a.dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(side), str(dest))
        for side in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            if side.exists():
                print(f"{'DRY ' if a.dry_run else ''}delete {side}")
                if not a.dry_run:
                    side.unlink()
    # tripwire units
    svc = UNITS / "mnemo-sqlite-tripwire.service"
    tmr = UNITS / "mnemo-sqlite-tripwire.timer"
    svc_text = (PLUGIN_DIR / "ops" / "mnemo-sqlite-tripwire.service").read_text()
    tmr_text = (PLUGIN_DIR / "ops" / "mnemo-sqlite-tripwire.timer").read_text()
    for f, text in ((svc, svc_text), (tmr, tmr_text)):
        if a.dry_run or os.environ.get("MNE_NO_SYSTEMD"):
            print(f"DRY would write {f}")
            continue
        if f.is_symlink() or (f.exists() and f.read_text() != text):
            f.unlink(missing_ok=True)
        if not f.exists():
            f.write_text(text)
            print(f"wrote {f}")
    if not a.dry_run:
        if os.environ.get("MNE_NO_SYSTEMD"):
            print("DRY(sysd): would daemon-reload + enable tripwire timer")
        else:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            subprocess.run(["systemctl", "--user", "enable", "--now",
                            "mnemo-sqlite-tripwire.timer"], check=False)
            print("tripwire timer enabled")
    print("retire_sqlite done", "(dry-run)" if a.dry_run else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())

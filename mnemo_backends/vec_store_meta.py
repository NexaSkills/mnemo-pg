"""Patch 1b: vec_store_meta — per-schema store metadata for the vec regime
classifier, stored in SQL so every connection agrees.

mnemosyne routes vec0 KNN via _classify_vec_store_regime(), which reads
PRAGMA user_version bit 0x20000000 (_VEC_NORM_BIT): marked = every stored row
is unit-normalized at insert time, so cosine-order KNN is exact. SQLite's
PRAGMA user_version is a per-database 64-bit integer; we emulate it with a
1-row meta table per schema (absent row = 1, sqlite's fresh-db value, which
keeps classification on the conservative 'legacy' arm).

The core's write path (_mark_vec_store_norm_bit) sets the bit via
'PRAGMA user_version = N'; _pragma() translates the assignment to an upsert.
Reads go through the same table.
"""
_META_TABLE = "mnemo_store_meta"


def ensure_meta_fn(base) -> None:
    base.execute(
        'CREATE TABLE IF NOT EXISTS "%s" (key text PRIMARY KEY, value bigint NOT NULL)'
        % _META_TABLE
    )


def user_version_select(base) -> str:
    """(sql, params) shaped like a one-cell PRAGMA read."""
    return ("SELECT COALESCE((SELECT value FROM \"%s\" WHERE key='user_version'), 1)::bigint"
            % _META_TABLE, ())


def user_version_assign(value: int) -> str:
    return ('INSERT INTO "%s"(key, value) VALUES (\'user_version\', %d) '
            'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value'
            % (_META_TABLE, int(value)))


def vec_version_select() -> str:
    """PRAGMA vec_version as a one-cell result.

    The core gates every sqlite-vec search behind _vec_available(), which
    probes vec_version()/vec_extension(). On PG the extension is
    emulated (pgvector rewriters), so report a plausibly modern version
    rather than failing the probe into the WHERE 0 degenerate path.
    """
    return ("SELECT 'v0.1.6-pgvector'::text", ())


def vec_extension_select() -> str:
    return ("SELECT 'sqlite-vec-emulated'::text", ())

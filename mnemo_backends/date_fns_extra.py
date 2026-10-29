"""SQLite date/time function parity for PostgreSQL (mnemo-pg).

The vendored mnemosyne core emits SQLite date idioms beyond julianday/
strftime: bare `datetime(x)`, `date(x)`, and `datetime('now')` (with and
without an SQLite modifier arg). These are installed idempotently on every
PG store connection (factory _ensure_date_fns) and by
scripts/date_fns_parity.py — both import THIS module so provisioning and
live paths stay identical.

Semantics matched to SQLite (UTC):
  datetime(x)       -> 'YYYY-MM-DD HH:MM:SS', NULL on garbage input
  date(x)           -> 'YYYY-MM-DD',         NULL on garbage input
  datetime('now', m)-> current time shifted by SQLite modifier m
                     (only the '-N <unit>' / '+N <unit>' day/hour/minute/
                      month/year forms are used by the core; anything else
                      is treated as a no-op modifier).

Input shapes accepted (mirroring julianday's acceptance set):
  NULL, epoch seconds (numeric or numeric text), ISO-8601 text
  ('YYYY-MM-DD', optional 'T'/' ' time, optional fractional seconds),
  native timestamp / timestamptz / date values.

NOTE: `time` is a reserved type name in PG; the core never calls time() in
SQL, so no time() parity function is defined.
"""

_NUMERIC = ("double precision", "real", "bigint", "integer", "smallint",
            "numeric")

_FUNCTIONS = []

# text input: epoch-numeric OR ISO datetime; NULL on garbage (SQLite parity)
_FUNCTIONS.append(r"""
CREATE OR REPLACE FUNCTION {schema}.datetime(x text) RETURNS text AS $fn$
  BEGIN
    IF x IS NULL OR btrim(x) = '' THEN RETURN NULL; END IF;
    IF lower(btrim(x)) IN ('now', 'subsec') THEN
      RETURN to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS');
    END IF;
    IF x ~ '^[[:space:]]*-?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?[[:space:]]*$' THEN
      RETURN to_char(to_date('1970-01-01','YYYY-MM-DD')
                     + (x::double precision / 86400.0) * interval '1 day',
                     'YYYY-MM-DD HH24:MI:SS');
    END IF;
    -- naive ISO text is epoch-UTC (SQLite parity); tz-aware keeps its offset
    RETURN CASE
      WHEN x ~ '[+Zz]$|[+][0-9]{2}:?[0-9]{2}$' THEN
        to_char((replace(btrim(x), 'T', ' '))::timestamptz
                AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
      ELSE
        to_char(to_timestamp(extract(epoch from
              (replace(btrim(x), 'T', ' '))::timestamp))
                AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
    END;
  EXCEPTION WHEN others THEN RETURN NULL;
  END
$fn$ LANGUAGE plpgsql STABLE PARALLEL SAFE
""")

_FUNCTIONS.append(r"""
CREATE OR REPLACE FUNCTION {schema}.datetime(x timestamp) RETURNS text AS $$
  SELECT CASE WHEN x IS NULL THEN NULL
         ELSE to_char(x AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') END
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE
""")

_FUNCTIONS.append(r"""
CREATE OR REPLACE FUNCTION {schema}.datetime(x timestamptz) RETURNS text AS $$
  SELECT CASE WHEN x IS NULL THEN NULL
         ELSE to_char(x AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') END
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE
""")

_FUNCTIONS.append(r"""
CREATE OR REPLACE FUNCTION {schema}.datetime(x date) RETURNS text AS $$
  SELECT CASE WHEN x IS NULL THEN NULL
         ELSE to_char(x, 'YYYY-MM-DD') || ' 00:00:00' END
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE
""")

# numeric shapes -> epoch seconds (same convention as julianday's
# double-precision overload). integer-family overloads prevent implicit
# resolution ambiguity against text (PG 18 resolves an unresolved prepared
# param to smallint).
for _num in _NUMERIC:
    # SQL function bodies are constant-folded at CREATE time, so every
    # reference to a schema-local function must be schema-qualified
    # (search_path does not apply during body inlining).
    _FUNCTIONS.append(r"""
CREATE OR REPLACE FUNCTION {schema}.datetime(x %s) RETURNS text AS $$
  SELECT CASE
    WHEN x IS NULL THEN NULL
    ELSE to_char(to_date('1970-01-01','YYYY-MM-DD')
                 + (x::double precision / 86400.0) * interval '1 day',
                 'YYYY-MM-DD HH24:MI:SS')
  END
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE
""" % _num)

# date(x): the DATE part of datetime(x)
_FUNCTIONS.append(r"""
CREATE OR REPLACE FUNCTION {schema}.date(x text) RETURNS text AS $fn$
  BEGIN
    RETURN CASE WHEN {schema}.datetime(x) IS NULL THEN NULL
           ELSE substr({schema}.datetime(x)::text, 1, 10) END;
  END
$fn$ LANGUAGE plpgsql STABLE PARALLEL SAFE
""")
_FUNCTIONS.append(r"""
CREATE OR REPLACE FUNCTION {schema}.date(x timestamp) RETURNS text AS $$
  SELECT CASE WHEN x IS NULL THEN NULL ELSE to_char(x, 'YYYY-MM-DD') END
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE
""")
_FUNCTIONS.append(r"""
CREATE OR REPLACE FUNCTION {schema}.date(x timestamptz) RETURNS text AS $$
  SELECT CASE WHEN x IS NULL THEN NULL
         ELSE to_char(x AT TIME ZONE 'UTC', 'YYYY-MM-DD') END
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE
""")
_FUNCTIONS.append(r"""
CREATE OR REPLACE FUNCTION {schema}.date(x date) RETURNS text AS $$
  SELECT CASE WHEN x IS NULL THEN NULL ELSE to_char(x, 'YYYY-MM-DD') END
$$ LANGUAGE sql IMMUTABLE PARALLEL SAFE
""")
for _num in _NUMERIC:
    _FUNCTIONS.append(r"""
CREATE OR REPLACE FUNCTION {schema}.date(x %s) RETURNS text AS $fn$
  BEGIN
    RETURN CASE WHEN {schema}.datetime(x::double precision) IS NULL THEN NULL
           ELSE substr({schema}.datetime(x::double precision)::text, 1, 10) END;
  END
$fn$ LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
""" % _num)

# datetime(x, mod): SQLite modifier form. The core uses datetime('now',
# '-7 days'). Support (text, text): apply the +/-N unit modifier when x is
# a 'now'-sentinel or a parseable datetime; unrecognized modifiers are a
# no-op (SQLite ignores them).
_FUNCTIONS.append(r"""
CREATE OR REPLACE FUNCTION {schema}.datetime(x text, mod text) RETURNS text AS $fn$
  DECLARE base_ts timestamptz;
          num bigint;
          unit text;
  BEGIN
    IF x IS NULL OR btrim(x) = '' THEN RETURN NULL; END IF;
    IF lower(btrim(x)) IN ('now', 'subsec') THEN
      base_ts := now();
    ELSIF x ~ '^[[:space:]]*-?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?[[:space:]]*$' THEN
      base_ts := to_timestamp(x::double precision);
    ELSIF x ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' THEN
      base_ts := ((replace(btrim(x), 'T', ' '))::timestamp
                  AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    ELSE
      RETURN NULL;
    END IF;
    IF mod IS NOT NULL AND mod ~ '^[[:space:]]*[+-][0-9]+[[:space:]]*[a-z]+' THEN
      num := (regexp_replace(btrim(mod), '^([+-][0-9]+).*', '\1'))::bigint;
      unit := lower(split_part(regexp_replace(btrim(mod), '^[+-]?[0-9]+[[:space:]]*', ''), ' ', 1));
      base_ts := base_ts + num * CASE
        WHEN unit IN ('day', 'days') THEN interval '1 day'
        WHEN unit IN ('hour', 'hours') THEN interval '1 hour'
        WHEN unit IN ('minute', 'minutes') THEN interval '1 minute'
        WHEN unit IN ('second', 'seconds') THEN interval '1 second'
        WHEN unit IN ('month', 'months') THEN interval '1 month'
        WHEN unit IN ('year', 'years') THEN interval '1 year'
        ELSE interval '0' END;
    END IF;
    RETURN to_char(base_ts AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS');
  END
$fn$ LANGUAGE plpgsql STABLE PARALLEL SAFE
""")


def sql_for(schema: str) -> list[str]:
    """Return the CREATE OR REPLACE statements for one schema."""
    return [f.replace("{schema}", '"%s"' % schema) for f in _FUNCTIONS]

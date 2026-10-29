"""Canonical SQLite date-function parity definitions (julianday, strftime).

The vendored core's temporal-validity predicates use julianday(); PG lacks
it. These CREATE OR REPLACE functions (idempotent, cheap) are installed on
every PG store connection by mnemo_backends.factory._ensure_date_fns and by
scripts/date_fns_parity.py -- both import THIS module so the definitions
never diverge. julianday(text) matches SQLite semantics: NULL on garbage
input, Julian-day scale, epoch-numeric or ISO-text input. PG 18 resolves an
unresolved prepared param to smallint, hence the smallint/int/bigint
overloads: without them julianday(?) falls into an ambiguous/none match.
"""

FUNCTIONS = (
    # epoch-seconds numeric forms -> Julian day number
    "CREATE OR REPLACE FUNCTION julianday(x double precision) RETURNS double precision "
    "LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ "
    "SELECT CASE WHEN x IS NULL THEN NULL ELSE x / 86400.0 + 2440587.5 END $$",
    "CREATE OR REPLACE FUNCTION julianday(x real) RETURNS double precision "
    "LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ "
    "SELECT CASE WHEN x IS NULL THEN NULL ELSE x::double precision / 86400.0 + 2440587.5 END $$",
    "CREATE OR REPLACE FUNCTION julianday(x bigint) RETURNS double precision "
    "LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ "
    "SELECT CASE WHEN x IS NULL THEN NULL ELSE x::double precision / 86400.0 + 2440587.5 END $$",
    "CREATE OR REPLACE FUNCTION julianday(x integer) RETURNS double precision "
    "LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ "
    "SELECT CASE WHEN x IS NULL THEN NULL ELSE x::double precision / 86400.0 + 2440587.5 END $$",
    "CREATE OR REPLACE FUNCTION julianday(x smallint) RETURNS double precision "
    "LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ "
    "SELECT CASE WHEN x IS NULL THEN NULL ELSE x::double precision / 86400.0 + 2440587.5 END $$",
    # timestamp forms
    "CREATE OR REPLACE FUNCTION julianday(x timestamp) RETURNS double precision "
    "LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ "
    "SELECT CASE WHEN x IS NULL THEN NULL "
    "ELSE extract(epoch from x) / 86400.0 + 2440587.5 END $$",
    "CREATE OR REPLACE FUNCTION julianday(x timestamp with time zone) RETURNS double precision "
    "LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ "
    "SELECT CASE WHEN x IS NULL THEN NULL "
    "ELSE extract(epoch from x) / 86400.0 + 2440587.5 END $$",
    "CREATE OR REPLACE FUNCTION julianday(x date) RETURNS double precision "
    "LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ "
    "SELECT CASE WHEN x IS NULL THEN NULL "
    "ELSE extract(epoch from x) / 86400.0 + 2440587.5 END $$",
    # text form: epoch-numeric OR ISO datetime; NULL on garbage (SQLite parity)
    "CREATE OR REPLACE FUNCTION julianday(x text) RETURNS double precision "
    "LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $fn$ "
    "BEGIN "
    "  IF x IS NULL OR btrim(x) = '' THEN RETURN NULL; END IF; "
    "  IF x ~ '^[[:space:]]*[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?[[:space:]]*$' THEN "
    "    RETURN x::double precision / 86400.0 + 2440587.5; "
    "  END IF; "
    "  RETURN extract(epoch from x::timestamp) / 86400.0 + 2440587.5; "
    "EXCEPTION WHEN others THEN RETURN NULL; "
    "END $fn$",
    # strftime(fmt, x): the core uses '%s' (epoch seconds) forms
    "CREATE OR REPLACE FUNCTION strftime(fmt text, x double precision) RETURNS text "
    "LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ "
    "SELECT CASE WHEN fmt = '%s' AND x IS NOT NULL THEN x::bigint::text ELSE NULL END $$",
    "CREATE OR REPLACE FUNCTION strftime(fmt text, x timestamp) RETURNS text "
    "LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ "
    "SELECT CASE WHEN fmt = '%s' AND x IS NOT NULL "
    "THEN floor(extract(epoch from x))::bigint::text ELSE NULL END $$",
    "CREATE OR REPLACE FUNCTION strftime(fmt text, x timestamp with time zone) RETURNS text "
    "LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ "
    "SELECT CASE WHEN fmt = '%s' AND x IS NOT NULL "
    "THEN floor(extract(epoch from x))::bigint::text ELSE NULL END $$",
    "CREATE OR REPLACE FUNCTION strftime(fmt text, x text) RETURNS text "
    "LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $fn$ "
    "BEGIN "
    "  IF fmt = '%s' AND x IS NOT NULL AND btrim(x) <> '' THEN "
    "    IF x ~ '^[[:space:]]*[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?[[:space:]]*$' THEN "
    "      RETURN x::double precision::bigint::text; "
    "    END IF; "
    "    RETURN floor(extract(epoch from x::timestamp))::bigint::text; "
    "  END IF; "
    "  RETURN NULL; "
    "EXCEPTION WHEN others THEN RETURN NULL; "
    "END $fn$",
)

-- Fix working_memory.pinned on legacy schemas where the boolean twin has NO
-- default: converge_nullable_default_shape.py only considers columns with a
-- declared DEFAULT, so a bare `pinned boolean` (left by old DROP DEFAULT
-- runs) stays boolean forever. The vendored core's SQLite-shaped SQL
-- compares literally (`pinned = 0`) and writes `SET pinned = 1`; PG has no
-- boolean<->integer operator/cast, so those statements fail with
-- "operator does not exist: boolean = integer" / "column pinned is of type
-- boolean but expression is of type smallint".
-- Fix: boolean -> bigint. The USING CASE WHEN must qualify the column
-- (schema.table.pinned) so it binds to the OLD boolean value and not the
-- rewritten integer target (unqualified names resolve to the NEW column
-- inside ALTER TABLE ... TYPE USING). Then DEFAULT 0 to match the core's
-- INTEGER DEFAULT 0 declaration. Idempotent.
DO $pinfix$
DECLARE s text;
BEGIN
  FOR s IN
    SELECT n.nspname
    FROM pg_namespace n
    JOIN pg_class t ON t.relnamespace = n.oid AND t.relname = 'working_memory'
    JOIN pg_attribute a ON a.attrelid = t.oid AND a.attname = 'pinned'
      AND a.attnum > 0 AND NOT a.attisdropped
    WHERE n.nspname LIKE 'mnemosyne!_%' ESCAPE '!'
      AND format_type(a.atttypid, a.atttypmod) = 'boolean'
  LOOP
    EXECUTE format(
      'ALTER TABLE %1$I.working_memory ALTER COLUMN pinned TYPE bigint '
      'USING CASE WHEN %1$I.working_memory.pinned THEN 1 ELSE 0 END', s);
    EXECUTE format('ALTER TABLE %I.working_memory ALTER COLUMN pinned '
                   'SET DEFAULT 0', s);
    RAISE NOTICE 'PINFIX %: boolean -> bigint DEFAULT 0', s;
  END LOOP;
END
$pinfix$;

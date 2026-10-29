"""Replace shim_ddl's _mnemo_triggerdef with a plpgsql version.

The SQL-language variant couldn't strip the /*mnemo-event ...*/ sentinel
(regexp_replace's pattern string '/[*]...' fails to match through psycopg
parameterization), so use plpgsql with a WHILE-loop strip and straightforward
string ops.
"""
import psycopg

PLPGSQL = r'''CREATE OR REPLACE FUNCTION "{schema}"._mnemo_triggerdef(tg oid) RETURNS text
  LANGUAGE plpgsql STABLE AS $td$
DECLARE
  t record;
  p0 record;
  body text;
  res text;
  ev text := '';
  guard text;
  ofs text := NULL;
  vlen int;
  g text;
  gpos int;
  attn int;
  ord int := 0;
  found_ord int;
BEGIN
  SELECT * INTO t FROM pg_trigger tt WHERE tt.oid = tg;
  IF NOT FOUND THEN RETURN NULL; END IF;
  SELECT * INTO p0 FROM pg_proc pp
   WHERE pp.proname = '_trg_' || t.tgname ORDER BY pp.oid LIMIT 1;

  -- event kind from the sentinel stamped by the translator
  g := p0.prosrc;
  IF g LIKE '%/*mnemo-event: %' THEN
    gpos := position('/*mnemo-event: ' in g) + 13;
    ev := upper(substring(g from gpos for 6));
  ELSIF p0.prosrc ~ 'old[.]' AND p0.prosrc !~ 'new[.]' THEN
    ev := 'DELETE';
  ELSIF p0.prosrc ~ 'new[.]' AND p0.prosrc !~ 'old[.]' THEN
    ev := 'INSERT';
  ELSE
    ev := 'UPDATE';
  END IF;

  -- header: DELETE -> BEFORE, others -> AFTER
  res := 'CREATE TRIGGER ' || t.tgname || ' '
      || CASE WHEN ev = 'DELETE' THEN 'BEFORE ' ELSE 'AFTER ' END
      || ev;

  -- UPDATE OF list: varattno first-appearance order from the WHEN guard,
  -- skipping attno 1 (node_id is the trigger's own key, never in OF).
  IF ev = 'UPDATE' THEN
    guard := t.tgqual::text;
    ord := 0;
    WHILE guard IS NOT NULL AND position('varattno ' in guard) > 0 LOOP
      ord := ord + 1;
      gpos := position('varattno ' in guard) + 9;
      attn := NULL;
      SELECT (substring(guard from gpos for n)::int) INTO attn
      FROM generate_series(1,3) n
      WHERE substring(guard from gpos + n - 1 for 1) ~ '[0-9]'
        AND (substring(guard from gpos + n for 1) IS NULL
             OR substring(guard from gpos + n for 1) !~ '[0-9]')
      ORDER BY n LIMIT 1;
      IF attn IS NOT NULL AND attn > 1 THEN
        SELECT a.attname INTO g FROM pg_attribute a
         WHERE a.attrelid = t.tgrelid AND a.attnum = attn;
        IF g IS NOT NULL AND COALESCE(ofs,'') <> '' THEN
          -- dedupe: only append if not already listed
          SELECT count(*) INTO found_ord FROM unnest(string_to_array(ofs, ',')) c
           WHERE c = g;
          IF found_ord = 0 THEN ofs := ofs || ',' || g; END IF;
        ELSIF g IS NOT NULL THEN
          ofs := g;
        END IF;
      END IF;
      guard := substring(guard from gpos);
    END LOOP;
    IF ofs IS NOT NULL THEN res := res || ' OF ' || ofs; END IF;
  END IF;

  res := res || ' ON ' || (SELECT relname FROM pg_class WHERE oid = t.tgrelid);

  -- body from the shadow function: strip wrapper, sentinel, normalize
  body := p0.prosrc;
  WHILE body LIKE '%/*mnemo-event:%' LOOP
    body := replace(body, substring(body from position('/*mnemo-event:' in body)
                           for position('*/' in substring(body from
                                  position('/*mnemo-event:' in body))) + 1), '');
  END LOOP;
  body := replace(body, 'RETURN NEW;', '');
  body := replace(body, 'RETURN OLD;', '');
  body := replace(body, 'NEW.', 'new.');
  body := replace(body, 'OLD.', 'old.');
  body := replace(body, 'greatest(', 'max(');
  body := replace(body, 'least(', 'min(');
  body := btrim(body);
  body := regexp_replace(body, '^[[:space:]]*BEGIN[[:space:]]*', '');
  body := regexp_replace(body, '[[:space:]]*END[[:space:]]*;?[[:space:]]*$', '');

  res := res || ' BEGIN ' || body || ' END';
  RETURN res;
END
$td$'''


def install(c, schema):
    c.execute(PLPGSQL.format(schema=schema))



# (lcm-pg original had an ad-hoc e2e self-check here; mnemo-pg tests live in tests/)

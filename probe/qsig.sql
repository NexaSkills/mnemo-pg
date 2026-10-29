-- qsig.sql — recall-signals check on a profile schema (run via pgq in schema).
-- marker: ZEBRA911 must be in working_memory + vec_working
\set QUIET on
SET search_path TO mnemosyne_mike, public;

-- 1) FTS arm via the shim view
SELECT 'fts' AS arm, count(*) AS n FROM fts_working WHERE fts_working MATCH 'zebra' ;
SELECT 'fts_zebra911' AS arm, count(*) AS n FROM fts_working WHERE fts_working MATCH 'zeb*' ;

-- 2) vector arm on vec_working via pgvector
SELECT 'vec_knn' AS arm, count(*) AS n FROM (
  SELECT wm.id, vw.embedding <=> (SELECT embedding FROM vec_working LIMIT 1) AS d
  FROM vec_working vw JOIN working_memory wm ON wm.working_memory__rowid = vw.rowid
  LIMIT 10) t;

-- 3) dense arm (numpy path in core = embeddings join)
SELECT 'dense_join' AS arm, count(*) AS n
FROM memory_embeddings me JOIN working_memory wm ON wm.id = me.memory_id
WHERE wm.consolidated_at IS NULL;

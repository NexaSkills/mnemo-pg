#!/bin/bash
# EXPLAIN the actual KNN query per profile schema to see index vs seq scan.
set -u
for p in mike default brandbuddy buttercup debbie skilltest; do
  echo "== mnemosyne_$p"
  psql "host=/home/mybuddy/lcm-pg/run port=54329 dbname=lcm user=lcm01" -qtA -c \
    "SET search_path TO mnemosyne_$p; EXPLAIN SELECT rowid, (embedding <=> '[0.1,0.2]'::vector) AS distance FROM vec_episodes ORDER BY distance LIMIT 5" 2>&1 | sed -n '1,6p'
done

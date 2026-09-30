SELECT id6, MAX(v3) AS max_v3, MIN(v3) AS min_v3, SUM(v3) AS sum_v3, COUNT(*) AS cnt FROM groupby WHERE v3 IS NOT NULL GROUP BY id6;

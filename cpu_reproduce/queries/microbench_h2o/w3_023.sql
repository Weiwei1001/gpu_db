-- -- q10: ~100 groups, multi-agg
-- Workload: w3 | Estimated GPU time: ~9.6ms

SELECT id1, COUNT(*), SUM(v1), AVG(v2), MIN(v3), MAX(v3) FROM groupby GROUP BY id1;

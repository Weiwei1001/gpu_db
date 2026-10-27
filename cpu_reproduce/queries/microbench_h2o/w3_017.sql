-- -- q9: ~100 groups (id2)
-- Workload: w3 | Estimated GPU time: ~8.0ms

SELECT id2, SUM(v1), AVG(v2) FROM groupby GROUP BY id2;

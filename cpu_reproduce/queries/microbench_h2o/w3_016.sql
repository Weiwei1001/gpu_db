-- -- q8: ~100 groups (id1)
-- Workload: w3 | Estimated GPU time: ~8.0ms

SELECT id1, SUM(v1) FROM groupby GROUP BY id1;

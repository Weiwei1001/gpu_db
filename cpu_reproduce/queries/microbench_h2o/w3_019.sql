-- -- q12: 2 keys, ~100x100 = ~10K groups
-- Workload: w3 | Estimated GPU time: ~8.0ms

SELECT id1, id2, SUM(v1) FROM groupby GROUP BY id1, id2;

-- -- q11: ~100 groups (id4 integer key)
-- Workload: w3 | Estimated GPU time: ~8.0ms

SELECT id4, SUM(v1), SUM(v2) FROM groupby GROUP BY id4;

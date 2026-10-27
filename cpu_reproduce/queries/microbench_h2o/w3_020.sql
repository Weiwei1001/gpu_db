-- -- q13: ~100 groups (id5)
-- Workload: w3 | Estimated GPU time: ~8.0ms

SELECT id5, AVG(v3) FROM groupby GROUP BY id5;

-- -- q9: ~100K groups (id6)
-- Workload: w4 | Estimated GPU time: ~32.0ms

SELECT id6, SUM(v1), SUM(v2) FROM groupby GROUP BY id6;

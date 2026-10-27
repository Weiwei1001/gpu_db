-- -- q10: ~100K groups, multi-agg
-- Workload: w4 | Estimated GPU time: ~76.8ms

SELECT id3, COUNT(*), AVG(v1), AVG(v2), AVG(v3) FROM groupby GROUP BY id3;

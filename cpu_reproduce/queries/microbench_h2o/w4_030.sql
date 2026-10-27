-- -- q13: ~100K groups single agg
-- Workload: w4 | Estimated GPU time: ~32.0ms

SELECT id3, COUNT(*) FROM groupby GROUP BY id3;

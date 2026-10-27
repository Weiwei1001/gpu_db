-- -- q8: ~100K groups (id3)
-- Workload: w4 | Estimated GPU time: ~32.0ms

SELECT id3, SUM(v1) FROM groupby GROUP BY id3;

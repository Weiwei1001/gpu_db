-- -- q11: compound key ~100 x 100K = very high cardinality
-- Workload: w4 | Estimated GPU time: ~64.0ms

SELECT id1, id3, SUM(v1) FROM groupby GROUP BY id1, id3;

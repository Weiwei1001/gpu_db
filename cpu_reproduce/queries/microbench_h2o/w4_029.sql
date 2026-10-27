-- -- q12: integer high cardinality
-- Workload: w4 | Estimated GPU time: ~32.0ms

SELECT id6, MIN(v3), MAX(v3) FROM groupby GROUP BY id6;

-- -- q13: very low selectivity ~0.1%
-- Workload: w2 | Estimated GPU time: ~4.8ms

SELECT MIN(v3), MAX(v3) FROM groupby WHERE id6 < 100;

-- -- q8: ~50% selectivity (numeric range)
-- Workload: w2 | Estimated GPU time: ~4.8ms

SELECT COUNT(*) FROM groupby WHERE id4 > 50;

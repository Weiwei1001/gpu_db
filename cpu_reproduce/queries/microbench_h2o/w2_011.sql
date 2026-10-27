-- -- q11: ~20% selectivity (numeric range)
-- Workload: w2 | Estimated GPU time: ~4.8ms

SELECT SUM(v1) FROM groupby WHERE id4 BETWEEN 10 AND 30;

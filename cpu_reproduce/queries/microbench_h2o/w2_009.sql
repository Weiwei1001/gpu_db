-- -- q9: ~50% selectivity (value filter)
-- Workload: w2 | Estimated GPU time: ~4.8ms

SELECT SUM(v1) FROM groupby WHERE v2 > 50;

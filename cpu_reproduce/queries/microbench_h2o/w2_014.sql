-- -- q14: high selectivity ~90%
-- Workload: w2 | Estimated GPU time: ~4.8ms

SELECT SUM(v1 + v2) FROM groupby WHERE v3 > -100;

-- -- q14: compound expression
-- Workload: w1 | Estimated GPU time: ~4.0ms

SELECT SUM(v1 + v2), AVG(v3 * v3) FROM groupby;

-- -- q12: compound filter
-- Workload: w2 | Estimated GPU time: ~4.8ms

SELECT COUNT(*) FROM groupby WHERE v1 > 0 AND v2 > 0;

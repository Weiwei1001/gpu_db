-- -- q19: high selectivity
-- Workload: w2 | Estimated GPU time: ~4.2ms

SELECT COUNT(*) FROM t WHERE UserID != 0;

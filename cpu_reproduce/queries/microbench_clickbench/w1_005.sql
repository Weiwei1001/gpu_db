-- -- q19: count + sum
-- Workload: w1 | Estimated GPU time: ~3.5ms

SELECT COUNT(*), SUM(GoodEvent) FROM t;

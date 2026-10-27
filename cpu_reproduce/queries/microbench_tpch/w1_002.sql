-- -- q7: orders table count + sum
-- Workload: w1 | Estimated GPU time: ~1.5ms

SELECT COUNT(*), SUM(o_totalprice) FROM orders;

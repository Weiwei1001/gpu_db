-- -- q7: orders filter
-- Workload: w2 | Estimated GPU time: ~1.8ms

SELECT COUNT(*), SUM(o_totalprice) FROM orders WHERE o_totalprice > 200000;

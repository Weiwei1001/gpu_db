-- -- q2: single column sum
-- Workload: w1 | Estimated GPU time: ~3.0ms

SELECT SUM(l_quantity) FROM lineitem;

-- -- q3: lineitem JOIN orders + filter on orders
-- Workload: w5a | Estimated GPU time: ~12.0ms

SELECT COUNT(*) FROM lineitem JOIN orders ON l_orderkey = o_orderkey WHERE o_orderstatus = 'F';

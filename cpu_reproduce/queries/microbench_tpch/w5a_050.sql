-- -- q8: 3-table: lineitem-orders-customer
-- Workload: w5a | Estimated GPU time: ~18.0ms

SELECT COUNT(*) FROM lineitem JOIN orders ON l_orderkey = o_orderkey JOIN customer ON o_custkey = c_custkey;

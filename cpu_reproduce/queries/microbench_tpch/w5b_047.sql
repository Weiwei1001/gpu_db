-- -- q6: 6-table: lineitem-orders-customer-supplier-nation-region
-- Workload: w5b | Estimated GPU time: ~16.2ms

SELECT r_name, COUNT(*), SUM(l_extendedprice), AVG(l_discount)
FROM lineitem
JOIN orders ON l_orderkey = o_orderkey
JOIN customer ON o_custkey = c_custkey
JOIN supplier ON l_suppkey = s_suppkey
JOIN nation ON s_nationkey = n_nationkey
JOIN region ON n_regionkey = r_regionkey
GROUP BY r_name;

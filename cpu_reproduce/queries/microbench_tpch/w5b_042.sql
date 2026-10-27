-- -- q5: 5-table: lineitem-orders-customer-supplier-nation (supplier side)
-- Workload: w5b | Estimated GPU time: ~13.5ms

SELECT n_name, SUM(l_extendedprice)
FROM lineitem
JOIN orders ON l_orderkey = o_orderkey
JOIN customer ON o_custkey = c_custkey
JOIN supplier ON l_suppkey = s_suppkey
JOIN nation ON s_nationkey = n_nationkey
GROUP BY n_name;

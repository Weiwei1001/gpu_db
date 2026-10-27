-- -- q10: 5-table with aggregation: lineitem-orders-customer + supplier-nation
-- Workload: w5b | Estimated GPU time: ~20.2ms

SELECT n_name, c_mktsegment, SUM(l_extendedprice)
FROM lineitem
JOIN orders ON l_orderkey = o_orderkey
JOIN customer ON o_custkey = c_custkey
JOIN supplier ON l_suppkey = s_suppkey
JOIN nation ON s_nationkey = n_nationkey
GROUP BY n_name, c_mktsegment;

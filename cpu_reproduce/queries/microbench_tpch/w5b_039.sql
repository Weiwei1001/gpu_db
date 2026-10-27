-- -- q1: 5-table: lineitem-orders-customer-nation-region
-- Workload: w5b | Estimated GPU time: ~13.5ms

SELECT r_name, COUNT(*), SUM(l_extendedprice)
FROM lineitem
JOIN orders ON l_orderkey = o_orderkey
JOIN customer ON o_custkey = c_custkey
JOIN nation ON c_nationkey = n_nationkey
JOIN region ON n_regionkey = r_regionkey
GROUP BY r_name;

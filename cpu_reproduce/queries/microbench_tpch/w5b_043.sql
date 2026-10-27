-- -- q7: 6-table + date filter
-- Workload: w5b | Estimated GPU time: ~13.5ms

SELECT r_name, SUM(l_extendedprice * (1 - l_discount))
FROM lineitem
JOIN orders ON l_orderkey = o_orderkey
JOIN customer ON o_custkey = c_custkey
JOIN supplier ON l_suppkey = s_suppkey
JOIN nation ON s_nationkey = n_nationkey
JOIN region ON n_regionkey = r_regionkey
WHERE o_orderdate >= '1995-01-01' AND o_orderdate < '1996-01-01'
GROUP BY r_name;

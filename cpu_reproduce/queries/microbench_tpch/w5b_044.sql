-- -- q8: 5-table: lineitem-partsupp-part-supplier-nation
-- Workload: w5b | Estimated GPU time: ~13.5ms

SELECT n_name, SUM(l_quantity), SUM(ps_supplycost)
FROM lineitem
JOIN partsupp ON l_partkey = ps_partkey AND l_suppkey = ps_suppkey
JOIN part ON l_partkey = p_partkey
JOIN supplier ON l_suppkey = s_suppkey
JOIN nation ON s_nationkey = n_nationkey
GROUP BY n_name;

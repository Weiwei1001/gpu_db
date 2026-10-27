-- -- q4: 5-table + filter on part
-- Workload: w5b | Estimated GPU time: ~13.5ms

SELECT n_name, SUM(l_quantity)
FROM lineitem
JOIN part ON l_partkey = p_partkey
JOIN supplier ON l_suppkey = s_suppkey
JOIN nation ON s_nationkey = n_nationkey
JOIN region ON n_regionkey = r_regionkey
WHERE p_size < 10
GROUP BY n_name;

-- -- q3: 5-table: lineitem-part-supplier-nation-region
-- Workload: w5b | Estimated GPU time: ~13.5ms

SELECT r_name, SUM(l_extendedprice)
FROM lineitem
JOIN part ON l_partkey = p_partkey
JOIN supplier ON l_suppkey = s_suppkey
JOIN nation ON s_nationkey = n_nationkey
JOIN region ON n_regionkey = r_regionkey
GROUP BY r_name;

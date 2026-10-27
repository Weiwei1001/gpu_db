-- -- q2: 5-table + filter on region
-- Workload: w5b | Estimated GPU time: ~9.0ms

SELECT SUM(l_extendedprice * (1 - l_discount))
FROM lineitem
JOIN orders ON l_orderkey = o_orderkey
JOIN customer ON o_custkey = c_custkey
JOIN nation ON c_nationkey = n_nationkey
JOIN region ON n_regionkey = r_regionkey
WHERE r_name = 'ASIA';

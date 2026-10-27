-- -- q6: lineitem JOIN part (~6M x 200K)
-- Workload: w5a | Estimated GPU time: ~12.0ms

SELECT SUM(l_extendedprice) FROM lineitem JOIN part ON l_partkey = p_partkey WHERE p_size < 10;

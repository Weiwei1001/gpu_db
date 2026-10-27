-- -- q7: lineitem JOIN supplier (~6M x 10K)
-- Workload: w5a | Estimated GPU time: ~18.0ms

SELECT s_nationkey, SUM(l_extendedprice) FROM lineitem JOIN supplier ON l_suppkey = s_suppkey GROUP BY s_nationkey;

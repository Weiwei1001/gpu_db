-- -- q2: lineitem JOIN orders + aggregation
-- Workload: w5a | Estimated GPU time: ~12.0ms

SELECT SUM(l_extendedprice) FROM lineitem JOIN orders ON l_orderkey = o_orderkey;

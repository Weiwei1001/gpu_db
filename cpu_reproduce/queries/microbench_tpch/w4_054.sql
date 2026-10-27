-- -- q3: ~1.5M groups (l_orderkey)
-- Workload: w4 | Estimated GPU time: ~24.0ms

SELECT l_orderkey, SUM(l_quantity), SUM(l_extendedprice) FROM lineitem GROUP BY l_orderkey;

-- -- q6: ~10K groups, multi-agg
-- Workload: w4 | Estimated GPU time: ~57.6ms

SELECT l_suppkey, MIN(l_extendedprice), MAX(l_extendedprice), AVG(l_quantity) FROM lineitem GROUP BY l_suppkey;

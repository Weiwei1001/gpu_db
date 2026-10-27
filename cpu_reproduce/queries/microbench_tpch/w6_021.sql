-- -- q7: sort + filter
-- Workload: w6 | Estimated GPU time: ~4.5ms

SELECT l_orderkey, l_extendedprice FROM lineitem WHERE l_returnflag = 'R' ORDER BY l_extendedprice DESC LIMIT 100;

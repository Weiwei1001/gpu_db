-- -- q2: top-10 by price
-- Workload: w6 | Estimated GPU time: ~6.0ms

SELECT l_orderkey, l_extendedprice FROM lineitem ORDER BY l_extendedprice DESC LIMIT 10;

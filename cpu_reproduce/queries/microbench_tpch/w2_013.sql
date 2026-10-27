-- -- q2: ~10% selectivity (narrow range)
-- Workload: w2 | Estimated GPU time: ~3.6ms

SELECT SUM(l_extendedprice) FROM lineitem WHERE l_discount BETWEEN 0.05 AND 0.07;

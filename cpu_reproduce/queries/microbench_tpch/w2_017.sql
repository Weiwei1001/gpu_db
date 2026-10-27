-- -- q6: high selectivity ~80%
-- Workload: w2 | Estimated GPU time: ~3.6ms

SELECT SUM(l_quantity) FROM lineitem WHERE l_extendedprice > 10000;

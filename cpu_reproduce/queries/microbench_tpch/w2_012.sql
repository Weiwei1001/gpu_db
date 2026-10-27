-- -- q1: ~50% selectivity (l_quantity > 25)
-- Workload: w2 | Estimated GPU time: ~3.6ms

SELECT COUNT(*) FROM lineitem WHERE l_quantity > 25;
